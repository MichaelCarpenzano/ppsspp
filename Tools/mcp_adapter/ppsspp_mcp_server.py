#!/usr/bin/env python3
"""PPSSPP debugger bridge as an MCP stdio server.

This server connects to PPSSPP's built-in debugger websocket (`/debugger`,
subprotocol `debugger.ppsspp.org`) and exposes an MCP tool surface:

- get_frame
- step_frame
- press_button
- get_state
- read_memory
- reset_game
- save_state
- load_state
- list_states

The implementation is dependency-light and uses MCP JSON-RPC over stdio with
LSP-style `Content-Length` framing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any, Callable

try:
    import websocket  # type: ignore
except Exception:  # pragma: no cover
    websocket = None


SERVER_NAME = "ppsspp-mcp"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_WS_URL = "ws://127.0.0.1:3250/debugger"

BUTTON_NAMES = {
    "cross",
    "circle",
    "triangle",
    "square",
    "up",
    "down",
    "left",
    "right",
    "start",
    "select",
    "home",
    "screen",
    "note",
    "ltrigger",
    "rtrigger",
    "hold",
    "wlan",
    "remote_hold",
    "vol_up",
    "vol_down",
    "disc",
    "memstick",
    "forward",
    "back",
    "playpause",
    "l2",
    "l3",
    "r2",
    "r3",
}


class PPSSPPError(RuntimeError):
    """Raised for debugger websocket protocol or runtime failures."""


class PPSSPPDebuggerClient:
    def __init__(
        self,
        ws_url: str,
        request_timeout: float,
        frame_timeout: float,
    ):
        self.ws_url = ws_url
        self.request_timeout = request_timeout
        self.frame_timeout = frame_timeout
        self.ws: Any | None = None
        self._deferred_events: list[dict[str, Any]] = []

    def connect(self) -> None:
        if self.ws is not None:
            return
        if websocket is None:
            raise PPSSPPError(
                "Missing dependency 'websocket-client'. "
                "Install requirements.txt first."
            )
        try:
            self.ws = websocket.create_connection(
                self.ws_url,
                subprotocols=["debugger.ppsspp.org"],
                timeout=self.request_timeout,
            )
            self.request(
                "version",
                {"name": SERVER_NAME, "version": SERVER_VERSION},
                timeout=self.request_timeout,
            )
        except Exception as exc:
            self.ws = None
            raise PPSSPPError(
                f"Failed to connect to PPSSPP debugger "
                f"at {self.ws_url}: {exc}"
            )

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        self._deferred_events.clear()

    def request(
        self,
        event: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.connect()
        ticket = str(uuid.uuid4())
        payload: dict[str, Any] = {"event": event, "ticket": ticket}
        if params:
            payload.update(params)

        self._send(payload)

        response = self._pull_event(
            lambda e: e.get("ticket") == ticket,
            timeout or self.request_timeout,
        )
        self._raise_for_error_event(response)
        if response.get("event") != event:
            raise PPSSPPError(
                "Unexpected debugger response event "
                f"'{response.get('event')}', expected '{event}'"
            )
        return response

    def fire_event(
        self,
        event: str,
        params: dict[str, Any] | None = None,
        timeout: float = 0.35,
    ) -> dict[str, Any] | None:
        """Send an event that may not respond.

        If the event does synchronously fail in PPSSPP, that error is surfaced.
        Otherwise, returns None after a short timeout.
        """
        self.connect()
        ticket = str(uuid.uuid4())
        payload: dict[str, Any] = {"event": event, "ticket": ticket}
        if params:
            payload.update(params)

        self._send(payload)

        try:
            response = self._pull_event(
                lambda e: e.get("ticket") == ticket,
                timeout,
            )
        except TimeoutError:
            return None

        self._raise_for_error_event(response)
        return response

    def wait_for_event(
        self,
        event_name: str,
        *,
        timeout: float,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        def _match(ev: dict[str, Any]) -> bool:
            if ev.get("event") != event_name:
                return False
            return predicate(ev) if predicate else True

        return self._pull_event(_match, timeout)

    def ensure_stepping(self) -> dict[str, Any]:
        status = self.request("cpu.status")
        if status.get("stepping"):
            return status

        self.fire_event("cpu.stepping")

        deadline = time.monotonic() + self.frame_timeout
        while time.monotonic() < deadline:
            status = self.request("cpu.status")
            if status.get("stepping"):
                return status
            time.sleep(0.02)

        raise TimeoutError("Timed out waiting for debugger stepping state")

    def step_frame(self, count: int) -> dict[str, Any]:
        if count < 1:
            raise PPSSPPError("step_frame count must be >= 1")

        last_status: dict[str, Any] | None = None
        for _ in range(count):
            self.ensure_stepping()

            status_before = self.request("cpu.status")
            prev_ticks = float(status_before.get("ticks", 0.0) or 0.0)

            self.request("cpu.stepFrame", timeout=self.request_timeout)

            deadline = time.monotonic() + self.frame_timeout
            while time.monotonic() < deadline:
                status_now = self.request("cpu.status")
                now_ticks = float(status_now.get("ticks", 0.0) or 0.0)
                if now_ticks > prev_ticks:
                    last_status = status_now
                    break
                time.sleep(0.01)
            else:
                raise TimeoutError("Timed out waiting for frame step")

        return {
            "count": count,
            "pc": last_status.get("pc") if last_status else None,
            "ticks": last_status.get("ticks") if last_status else None,
            "stepping": (
                bool(last_status.get("stepping"))
                if last_status
                else None
            ),
        }

    def get_frame(self, alpha: bool = False) -> dict[str, Any]:
        self.ensure_stepping()

        response: dict[str, Any] | None = None
        capture_events = ("gpu.buffer.screenshot", "gpu.buffer.renderColor")

        for capture_event in capture_events:
            for attempt in range(3):
                try:
                    response = self.request(
                        capture_event,
                        {"type": "uri", "alpha": bool(alpha)},
                        timeout=max(self.request_timeout, self.frame_timeout),
                    )
                    break
                except PPSSPPError as exc:
                    error_text = str(exc)
                    recoverable = (
                        "Could not download output" in error_text
                        or "Neither CPU or GPU is stepping" in error_text
                    )
                    if not recoverable:
                        raise
                    if attempt == 2:
                        continue
                    self.ensure_stepping()
                    self.step_frame(1)
            if response is not None:
                break

        if response is None:
            raise PPSSPPError("Could not retrieve screenshot")

        uri = str(response.get("uri", ""))
        prefix = "data:image/png;base64,"
        if not uri.startswith(prefix):
            raise PPSSPPError(
                "Unexpected screenshot payload: expected PNG data URI"
            )

        return {
            "width": response.get("width"),
            "height": response.get("height"),
            "isFramebuffer": bool(response.get("isFramebuffer", False)),
            "png_base64": uri[len(prefix):],
        }

    def press_button(
        self,
        button: str,
        *,
        pressed: bool | None = None,
        duration: int | None = None,
    ) -> dict[str, Any]:
        normalized = button.strip().lower()
        if normalized not in BUTTON_NAMES:
            raise PPSSPPError(f"Unsupported button '{button}'")

        if duration is not None:
            if duration < 1:
                raise PPSSPPError("duration must be >= 1")
            self.fire_event(
                "input.buttons.press",
                {"button": normalized, "duration": int(duration)},
                timeout=0.05,
            )
            return {
                "button": normalized,
                "mode": "press",
                "duration_frames": int(duration),
            }

        state = True if pressed is None else bool(pressed)
        self.request(
            "input.buttons.send",
            {"buttons": {normalized: state}},
            timeout=self.request_timeout,
        )
        return {
            "button": normalized,
            "mode": "set",
            "pressed": state,
        }

    def get_state(self) -> dict[str, Any]:
        cpu = self.request("cpu.status")
        game = self.request("game.status")
        return {
            "cpu": cpu,
            "game": game,
            "connected": True,
            "ws_url": self.ws_url,
        }

    def read_memory(
        self,
        address: int,
        size: int,
    ) -> dict[str, Any]:
        """Read *size* bytes from PSP RAM at *address*.

        Returns a dict with ``address``, ``size``, and ``base64`` (the raw
        bytes encoded as a base64 string, as returned by the debugger).
        Requires the CPU to be in stepping mode; this method calls
        ``ensure_stepping()`` automatically.
        """
        if size < 1 or size > 0x100000:  # 1 MiB cap
            raise PPSSPPError(
                f"read_memory size must be between 1 and 1048576, got {size}"
            )
        self.ensure_stepping()
        response = self.request(
            "memory.read",
            {"address": int(address), "size": int(size)},
            timeout=self.request_timeout,
        )
        return {
            "address": address,
            "size": size,
            "base64": response.get("base64", ""),
        }

    def reset_game(self) -> dict[str, Any]:
        """Reset (reboot) the currently-running PSP game.

        Sends ``game.reset`` and waits for a ``game.start`` event.
        Returns the game status after the reset is signalled.
        """
        self.fire_event("game.reset", timeout=0.5)
        try:
            self.wait_for_event(
                "game.start",
                timeout=self.frame_timeout,
            )
        except TimeoutError:
            pass  # reset may not broadcast game.start reliably
        return self.request("game.status")

    def save_state(self, slot: int) -> dict[str, Any]:
        """Save the current emulator state into an in-memory slot (0-4)."""
        if slot < 0 or slot > 4:
            raise PPSSPPError("save_state slot must be between 0 and 4")
        self.ensure_stepping()
        response = self.request(
            "savestate.save",
            {"slot": int(slot)},
            timeout=max(self.request_timeout, self.frame_timeout),
        )
        return {
            "slot": int(response.get("slot", slot)),
            "size": int(response.get("size", 0) or 0),
        }

    def load_state(self, slot: int) -> dict[str, Any]:
        """Load emulator state from an in-memory slot (0-4)."""
        if slot < 0 or slot > 4:
            raise PPSSPPError("load_state slot must be between 0 and 4")
        self.ensure_stepping()
        response = self.request(
            "savestate.load",
            {"slot": int(slot)},
            timeout=max(self.request_timeout, self.frame_timeout),
        )
        # After a load, enforce stepping state again before the next operation.
        self.ensure_stepping()
        return {
            "slot": int(response.get("slot", slot)),
            "size": int(response.get("size", 0) or 0),
        }

    def list_states(self) -> dict[str, Any]:
        """Return occupancy information for in-memory savestate slots."""
        response = self.request(
            "savestate.list",
            timeout=self.request_timeout,
        )
        slots = response.get("slots", [])
        if not isinstance(slots, list):
            slots = []
        return {"slots": slots}

    def _send(self, payload: dict[str, Any]) -> None:
        if self.ws is None:
            raise PPSSPPError("WebSocket is not connected")
        self.ws.send(json.dumps(payload, separators=(",", ":")))

    def _recv_json(self, timeout: float) -> dict[str, Any]:
        if self.ws is None:
            raise PPSSPPError("WebSocket is not connected")

        self.ws.settimeout(timeout)
        try:
            raw = self.ws.recv()
        except Exception as exc:
            if websocket is not None and isinstance(
                exc,
                websocket.WebSocketTimeoutException,
            ):
                raise TimeoutError(
                    "Timed out waiting for debugger event"
                ) from exc
            raise

        if raw == "":
            raise TimeoutError("Timed out waiting for debugger event")

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PPSSPPError(f"Invalid JSON received from debugger: {exc}")

        if not isinstance(parsed, dict):
            raise PPSSPPError(
                "Unexpected non-object message received from debugger"
            )
        return parsed

    def _pull_event(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout

        while True:
            for i, ev in enumerate(self._deferred_events):
                if predicate(ev):
                    return self._deferred_events.pop(i)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for debugger event")

            try:
                event = self._recv_json(remaining)
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Timed out waiting for debugger event"
                    )
                continue
            if predicate(event):
                return event

            self._deferred_events.append(event)

    @staticmethod
    def _raise_for_error_event(event: dict[str, Any]) -> None:
        if event.get("event") == "error":
            raise PPSSPPError(
                str(event.get("message", "Unknown debugger error"))
            )


def read_mcp_message(stdin_buffer: Any) -> dict[str, Any] | None:
    headers: dict[str, str] = {}

    while True:
        line = stdin_buffer.readline()
        if not line:
            return None

        if line in (b"\r\n", b"\n"):
            break

        try:
            header_line = line.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError:
            continue

        if not header_line or ":" not in header_line:
            continue

        key, value = header_line.split(":", 1)
        headers[key.strip().lower()] = value.strip()

    content_length = headers.get("content-length")
    if content_length is None:
        return None

    try:
        n = int(content_length)
    except ValueError:
        return None

    if n <= 0:
        return None

    body = stdin_buffer.read(n)
    if not body:
        return None

    return json.loads(body.decode("utf-8"))


def write_mcp_message(stdout_buffer: Any, payload: dict[str, Any]) -> None:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stdout_buffer.write(header)
    stdout_buffer.write(body)
    stdout_buffer.flush()


def jsonrpc_response(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_frame",
            "description": "Get the current PPSSPP frame as PNG image data.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "alpha": {
                        "type": "boolean",
                        "description": (
                            "Include alpha channel in PNG generation."
                        ),
                        "default": False,
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "step_frame",
            "description": (
                "Advance emulation deterministically by N frames "
                "(default: 1)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "press_button",
            "description": "Set or pulse a PSP button state.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "button": {
                        "type": "string",
                        "description": (
                            "PSP button name (cross, circle, up, "
                            "start, ltrigger, etc)."
                        ),
                    },
                    "pressed": {
                        "type": "boolean",
                        "description": (
                            "Button state for set mode (default: true)."
                        ),
                    },
                    "duration": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "If provided, pulse the button for "
                            "this many frames."
                        ),
                    },
                },
                "required": ["button"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_state",
            "description": "Fetch current PPSSPP CPU/game status.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "read_memory",
            "description": (
                "Read raw bytes from PSP RAM at a given address. "
                "Returns base64-encoded binary data."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "address": {
                        "type": "integer",
                        "description": (
                            "PSP virtual address (e.g. 0x08000000 for "
                            "start of RAM)."
                        ),
                    },
                    "size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1048576,
                        "description": "Number of bytes to read (max 1 MiB).",
                    },
                },
                "required": ["address", "size"],
                "additionalProperties": False,
            },
        },
        {
            "name": "reset_game",
            "description": (
                "Reboot the currently-loaded PSP game from the beginning."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "save_state",
            "description": (
                "Save emulator state to an in-memory savestate slot (0-4)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slot": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4,
                        "default": 0,
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "load_state",
            "description": (
                "Load emulator state from an in-memory savestate slot (0-4)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slot": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4,
                        "default": 0,
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "list_states",
            "description": "List in-memory savestate slot occupancy.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]


def text_tool_result(data: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = (
        data
        if isinstance(data, str)
        else json.dumps(data, ensure_ascii=False)
    )
    return {
        "isError": is_error,
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
    }


def image_tool_result(frame: dict[str, Any]) -> dict[str, Any]:
    meta = {
        "width": frame.get("width"),
        "height": frame.get("height"),
        "isFramebuffer": frame.get("isFramebuffer", False),
    }
    return {
        "isError": False,
        "content": [
            {
                "type": "image",
                "mimeType": "image/png",
                "data": frame["png_base64"],
            },
            {
                "type": "text",
                "text": json.dumps(meta, ensure_ascii=False),
            },
        ],
    }


def call_tool(
    client: PPSSPPDebuggerClient,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if name == "get_frame":
        frame = client.get_frame(alpha=bool(arguments.get("alpha", False)))
        return image_tool_result(frame)

    if name == "step_frame":
        count = int(arguments.get("count", 1))
        result = client.step_frame(count)
        return text_tool_result(result)

    if name == "press_button":
        if "button" not in arguments:
            raise PPSSPPError("Missing required argument 'button'")
        button = str(arguments["button"])
        duration = arguments.get("duration")
        pressed = arguments.get("pressed")

        duration_int = int(duration) if duration is not None else None
        pressed_bool = bool(pressed) if pressed is not None else None

        result = client.press_button(
            button,
            pressed=pressed_bool,
            duration=duration_int,
        )
        return text_tool_result(result)

    if name == "get_state":
        return text_tool_result(client.get_state())

    if name == "read_memory":
        if "address" not in arguments or "size" not in arguments:
            raise PPSSPPError(
                "read_memory requires 'address' and 'size' arguments"
            )
        result = client.read_memory(
            int(arguments["address"]),
            int(arguments["size"]),
        )
        return text_tool_result(result)

    if name == "reset_game":
        return text_tool_result(client.reset_game())

    if name == "save_state":
        slot = int(arguments.get("slot", 0))
        return text_tool_result(client.save_state(slot))

    if name == "load_state":
        slot = int(arguments.get("slot", 0))
        return text_tool_result(client.load_state(slot))

    if name == "list_states":
        return text_tool_result(client.list_states())

    raise PPSSPPError(f"Unknown tool '{name}'")


def run_server(
    ws_url: str,
    request_timeout: float,
    frame_timeout: float,
) -> int:
    client = PPSSPPDebuggerClient(ws_url, request_timeout, frame_timeout)

    stdin_buffer = sys.stdin.buffer
    stdout_buffer = sys.stdout.buffer

    while True:
        try:
            msg = read_mcp_message(stdin_buffer)
        except json.JSONDecodeError as exc:
            write_mcp_message(
                stdout_buffer,
                jsonrpc_error(None, -32700, f"Parse error: {exc}"),
            )
            continue
        except Exception as exc:
            write_mcp_message(
                stdout_buffer,
                jsonrpc_error(None, -32603, f"Read error: {exc}"),
            )
            continue

        if msg is None:
            break

        if not isinstance(msg, dict):
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params", {})
        if not isinstance(params, dict):
            params = {}

        if not method:
            if req_id is not None:
                write_mcp_message(
                    stdout_buffer,
                    jsonrpc_error(req_id, -32600, "Invalid Request"),
                )
            continue

        if method == "initialize":
            protocol_version = params.get(
                "protocolVersion",
                DEFAULT_PROTOCOL_VERSION,
            )
            result = {
                "protocolVersion": protocol_version,
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            }
            write_mcp_message(stdout_buffer, jsonrpc_response(req_id, result))
            continue

        if method == "notifications/initialized":
            continue

        if method == "ping":
            if req_id is not None:
                write_mcp_message(stdout_buffer, jsonrpc_response(req_id, {}))
            continue

        if method == "tools/list":
            if req_id is not None:
                write_mcp_message(
                    stdout_buffer,
                    jsonrpc_response(req_id, {"tools": tool_definitions()}),
                )
            continue

        if method == "tools/call":
            if req_id is None:
                continue

            tool_name = params.get("name")
            args = params.get("arguments", {})
            if not isinstance(tool_name, str):
                write_mcp_message(
                    stdout_buffer,
                    jsonrpc_response(
                        req_id,
                        text_tool_result(
                            "Invalid or missing tool name",
                            is_error=True,
                        ),
                    ),
                )
                continue
            if not isinstance(args, dict):
                args = {}

            try:
                result = call_tool(client, tool_name, args)
            except Exception as exc:
                result = text_tool_result(str(exc), is_error=True)

            write_mcp_message(stdout_buffer, jsonrpc_response(req_id, result))
            continue

        if method in ("shutdown",):
            if req_id is not None:
                write_mcp_message(stdout_buffer, jsonrpc_response(req_id, {}))
            continue

        if method in ("exit",):
            break

        if req_id is not None:
            write_mcp_message(
                stdout_buffer,
                jsonrpc_error(
                    req_id,
                    -32601,
                    f"Method not found: {method}",
                ),
            )

    client.close()
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PPSSPP debugger MCP adapter")
    parser.add_argument(
        "--ws-url",
        default=os.environ.get("PPSSPP_DEBUGGER_WS", DEFAULT_WS_URL),
        help="Debugger websocket URL (default: %(default)s)",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=float(os.environ.get("PPSSPP_DEBUGGER_TIMEOUT", "6.0")),
        help="Per-request timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--frame-timeout",
        type=float,
        default=float(os.environ.get("PPSSPP_DEBUGGER_FRAME_TIMEOUT", "15.0")),
        help=(
            "Timeout for frame-step/screenshot waits in seconds "
            "(default: %(default)s)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    return run_server(args.ws_url, args.request_timeout, args.frame_timeout)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
