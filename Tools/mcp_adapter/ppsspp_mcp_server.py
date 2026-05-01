#!/usr/bin/env python3
"""PPSSPP debugger bridge as an MCP stdio server.

This server connects to PPSSPP's built-in debugger websocket (`/debugger`,
subprotocol `debugger.ppsspp.org`) and exposes an MCP tool surface:

- frame/state/memory inspection and stepping controls
- input recording and replay
- trace sessions with JSONL streams and manifests
- hash probes and first-divergence trace diffing
- read-only MCP resources for capabilities, session, game, memory map, and
  trace schema

The implementation is dependency-light and uses MCP JSON-RPC over stdio with
LSP-style `Content-Length` framing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
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
TRACE_SCHEMA_VERSION = "ppsspp.trace.v1"
MAX_FRAME_STEP_COUNT = 600
MAX_REPLAY_FRAMES = 20000
MAX_TRACE_FRAMES = 20000
MAX_MEMORY_READ_SIZE = 0x100000
MAX_EXPORT_BYTES = 64 * 1024 * 1024
DEFAULT_ARTIFACT_ROOT = Path(
    os.environ.get(
        "PPSSPP_MCP_ARTIFACT_ROOT",
        str(Path(__file__).resolve().parent / "artifacts"),
    )
)

LOG = logging.getLogger(SERVER_NAME)

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


class ToolValidationError(PPSSPPError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str = "",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.retryable = retryable


class BackendUnavailableError(PPSSPPError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class RequestTimeoutError(TimeoutError):
    pass


class FrameStepTimeoutError(TimeoutError):
    pass


class TraceFlushTimeoutError(TimeoutError):
    pass


def structured_error(
    exc: Exception,
    *,
    default_code: str = "internal_error",
) -> dict[str, Any]:
    if isinstance(exc, ToolValidationError):
        return {
            "code": exc.code,
            "message": exc.message,
            "hint": exc.hint,
            "retryable": exc.retryable,
        }
    if isinstance(exc, BackendUnavailableError):
        return {
            "code": "backend_unavailable",
            "message": str(exc),
            "hint": (
                "Start PPSSPP with the debugger websocket enabled and check "
                "PPSSPP_DEBUGGER_WS."
            ),
            "retryable": exc.retryable,
        }
    if isinstance(exc, RequestTimeoutError):
        return {
            "code": "request_timeout",
            "message": str(exc),
            "hint": "Retry the call or increase PPSSPP_DEBUGGER_TIMEOUT.",
            "retryable": True,
        }
    if isinstance(exc, FrameStepTimeoutError):
        return {
            "code": "frame_step_timeout",
            "message": str(exc),
            "hint": "Check that PPSSPP is paused/stepping and not hung.",
            "retryable": True,
        }
    if isinstance(exc, TraceFlushTimeoutError):
        return {
            "code": "trace_flush_timeout",
            "message": str(exc),
            "hint": "Retry export with a smaller trace or writable output dir.",
            "retryable": True,
        }
    return {
        "code": default_code,
        "message": str(exc),
        "hint": "",
        "retryable": False,
    }


def now_ms() -> int:
    return int(time.time() * 1000)


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(data: Any) -> str:
    return sha256_bytes(canonical_json(data).encode("utf-8"))


def normalize_for_diff(data: Any, *, mask_addresses: bool = False) -> Any:
    unstable_keys = {
        "timestamp",
        "timestamp_ms",
        "time",
        "host_path",
        "path",
        "run_id",
        "ticket",
    }
    pointer_re = re.compile(r"0x[0-9a-fA-F]{7,16}")

    if isinstance(data, dict):
        return {
            key: normalize_for_diff(value, mask_addresses=mask_addresses)
            for key, value in sorted(data.items())
            if key not in unstable_keys
        }
    if isinstance(data, list):
        return [normalize_for_diff(v, mask_addresses=mask_addresses) for v in data]
    if mask_addresses and isinstance(data, str):
        return pointer_re.sub("0xADDR", data)
    return data


def require_no_extra(arguments: dict[str, Any], allowed: set[str]) -> None:
    extra = sorted(set(arguments) - allowed)
    if extra:
        raise ToolValidationError(
            "invalid_argument",
            f"Unknown argument(s): {', '.join(extra)}",
            hint=f"Allowed arguments: {', '.join(sorted(allowed))}",
        )


def require_int(
    arguments: dict[str, Any],
    name: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if name not in arguments:
        if default is None:
            raise ToolValidationError(
                "missing_argument",
                f"Missing required argument '{name}'",
            )
        return default
    value = arguments[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolValidationError(
            "invalid_argument",
            f"Argument '{name}' must be an integer",
        )
    if minimum is not None and value < minimum:
        raise ToolValidationError(
            "invalid_argument",
            f"Argument '{name}' must be >= {minimum}",
        )
    if maximum is not None and value > maximum:
        raise ToolValidationError(
            "limit_exceeded",
            f"Argument '{name}' must be <= {maximum}",
        )
    return value


def require_bool(
    arguments: dict[str, Any],
    name: str,
    *,
    default: bool = False,
) -> bool:
    if name not in arguments:
        return default
    value = arguments[name]
    if not isinstance(value, bool):
        raise ToolValidationError(
            "invalid_argument",
            f"Argument '{name}' must be a boolean",
        )
    return value


def require_str(
    arguments: dict[str, Any],
    name: str,
    *,
    default: str | None = None,
    non_empty: bool = True,
) -> str:
    if name not in arguments:
        if default is None:
            raise ToolValidationError(
                "missing_argument",
                f"Missing required argument '{name}'",
            )
        return default
    value = arguments[name]
    if not isinstance(value, str):
        raise ToolValidationError(
            "invalid_argument",
            f"Argument '{name}' must be a string",
        )
    if non_empty and not value.strip():
        raise ToolValidationError(
            "invalid_argument",
            f"Argument '{name}' must not be empty",
        )
    return value


def require_string_list(
    arguments: dict[str, Any],
    name: str,
    *,
    default: list[str],
    allowed: set[str] | None = None,
) -> list[str]:
    if name not in arguments:
        return list(default)
    value = arguments[name]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolValidationError(
            "invalid_argument",
            f"Argument '{name}' must be a list of strings",
        )
    normalized = [v.strip().lower() for v in value]
    if allowed is not None:
        unknown = sorted(set(normalized) - allowed)
        if unknown:
            raise ToolValidationError(
                "invalid_argument",
                f"Unknown {name}: {', '.join(unknown)}",
                hint=f"Allowed values: {', '.join(sorted(allowed))}",
            )
    return normalized


def resolve_artifact_path(path_text: str, *, must_exist: bool = False) -> Path:
    root = DEFAULT_ARTIFACT_ROOT.resolve()
    candidate = Path(path_text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolValidationError(
            "invalid_path",
            f"Path is outside the allowed artifact root: {resolved}",
            hint=f"Use a path under {root}",
        ) from exc
    if must_exist and not resolved.exists():
        raise ToolValidationError(
            "invalid_path",
            f"Path does not exist: {resolved}",
        )
    return resolved


def ensure_output_dir(path_text: str | None) -> Path:
    if path_text is not None and not isinstance(path_text, str):
        raise ToolValidationError(
            "invalid_argument",
            "Argument 'output_dir' must be a string",
        )
    if path_text:
        output_dir = resolve_artifact_path(path_text)
    else:
        output_dir = DEFAULT_ARTIFACT_ROOT.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


class InputRecorder:
    def __init__(self) -> None:
        self.active = False
        self.run_id = ""
        self.start_frame = 0
        self.events: list[dict[str, Any]] = []

    def start(self, start_frame: int) -> dict[str, Any]:
        if self.active:
            raise ToolValidationError(
                "session_active",
                "Input recording is already active",
            )
        self.active = True
        self.run_id = f"inputs-{uuid.uuid4().hex[:12]}"
        self.start_frame = start_frame
        self.events = []
        return {
            "run_id": self.run_id,
            "start_frame": self.start_frame,
            "recording": True,
        }

    def stop(self, end_frame: int) -> dict[str, Any]:
        if not self.active:
            raise ToolValidationError(
                "session_inactive",
                "Input recording is not active",
            )
        self.active = False
        return {
            "run_id": self.run_id,
            "start_frame": self.start_frame,
            "end_frame": end_frame,
            "event_count": len(self.events),
            "recording": False,
        }

    def append(
        self,
        frame: int,
        button: str,
        action: str,
        *,
        duration: int | None = None,
    ) -> None:
        if not self.active:
            return
        event: dict[str, Any] = {
            "frame": int(frame),
            "button": button,
            "action": action,
        }
        if duration is not None:
            event["duration"] = int(duration)
        self.events.append(event)

    def export(self, output_dir: Path) -> dict[str, Any]:
        if not self.run_id:
            raise ToolValidationError(
                "session_inactive",
                "No input recording has been started",
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{self.run_id}.inputs.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for event in self.events:
                f.write(canonical_json(event) + "\n")
        return {
            "path": str(path),
            "run_id": self.run_id,
            "event_count": len(self.events),
            "sha256": sha256_bytes(path.read_bytes()),
        }


class TraceSession:
    def __init__(
        self,
        *,
        subsystems: list[str],
        start_frame: int,
        max_frames: int,
        output_dir: Path,
    ) -> None:
        self.run_id = f"trace-{uuid.uuid4().hex[:12]}"
        self.subsystems = subsystems
        self.start_frame = start_frame
        self.max_frames = max_frames
        self.output_dir = output_dir / self.run_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.stream_paths = {
            subsystem: self.output_dir / f"{subsystem}.jsonl"
            for subsystem in subsystems
        }
        self.counts = {subsystem: 0 for subsystem in subsystems}
        self.end_frame = start_frame
        self.active = True

    def record(self, subsystem: str, event: dict[str, Any]) -> None:
        if not self.active or subsystem not in self.stream_paths:
            return
        if self.total_events >= self.max_frames * max(1, len(self.subsystems)):
            self.active = False
            return
        with self.stream_paths[subsystem].open("a", encoding="utf-8") as f:
            f.write(canonical_json(event) + "\n")
        self.counts[subsystem] += 1
        frame = event.get("frame")
        if isinstance(frame, int):
            self.end_frame = max(self.end_frame, frame)

    @property
    def total_events(self) -> int:
        return sum(self.counts.values())

    def stop(self, end_frame: int) -> dict[str, Any]:
        self.active = False
        self.end_frame = max(self.end_frame, end_frame)
        return self.summary()

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "active": self.active,
            "subsystems": self.subsystems,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "event_counts": dict(self.counts),
            "output_dir": str(self.output_dir),
        }

    def manifest(
        self,
        *,
        game: dict[str, Any],
        build_id: str,
        settings_fingerprint: str,
    ) -> dict[str, Any]:
        files: dict[str, dict[str, Any]] = {}
        total_size = 0
        for subsystem, path in self.stream_paths.items():
            if not path.exists():
                path.touch()
            size = path.stat().st_size
            total_size += size
            if total_size > MAX_EXPORT_BYTES:
                raise ToolValidationError(
                    "limit_exceeded",
                    "Trace export exceeds the maximum payload size",
                    hint="Use max_frames or subsystem filtering to reduce trace size.",
                )
            files[subsystem] = {
                "path": str(path),
                "sha256": sha256_bytes(path.read_bytes()),
                "bytes": size,
                "events": self.counts.get(subsystem, 0),
            }

        return {
            "schema": TRACE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "game_id": (
                game.get("id")
                or game.get("gameId")
                or game.get("title")
                or "unknown"
            ),
            "build_id": build_id,
            "ppsspp_version": game.get("version") or game.get("ppssppVersion") or "unknown",
            "settings_fingerprint": settings_fingerprint,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "subsystems": self.subsystems,
            "files": files,
        }


class AdapterSession:
    def __init__(self, client: PPSSPPDebuggerClient) -> None:
        self.client = client
        self.recorder = InputRecorder()
        self.trace: TraceSession | None = None
        self.loaded_inputs: list[dict[str, Any]] = []
        self.loaded_inputs_path: str | None = None

    def current_frame(self, *, strict: bool = True) -> int:
        try:
            status = self.client.request("cpu.status", timeout=self.client.request_timeout)
        except Exception as exc:
            if strict:
                raise
            LOG.debug("current frame unavailable: %s", exc)
            return 0
        for key in ("frame", "frameIndex", "frameCounter"):
            value = status.get(key)
            if isinstance(value, int):
                return value
        ticks = status.get("ticks")
        if isinstance(ticks, (int, float)):
            return int(ticks)
        return 0

    def record_trace_probe(self) -> None:
        trace = self.trace
        if trace is None or not trace.active:
            return
        frame = self.current_frame(strict=False)
        timestamp = now_ms()
        try:
            if "frame_hash" in trace.subsystems or "ge" in trace.subsystems:
                frame_hash = self.frame_hash()
                event = {
                    "frame": frame,
                    "timestamp_ms": timestamp,
                    "kind": "frame_hash",
                    "sha256": frame_hash["sha256"],
                    "width": frame_hash.get("width"),
                    "height": frame_hash.get("height"),
                }
                if "frame_hash" in trace.subsystems:
                    trace.record("frame_hash", event)
                if "ge" in trace.subsystems:
                    trace.record("ge", event)
            if "state" in trace.subsystems:
                state = self.client.get_state()
                trace.record(
                    "state",
                    {
                        "frame": frame,
                        "timestamp_ms": timestamp,
                        "kind": "state",
                        "fingerprint": sha256_json(normalize_for_diff(state)),
                        "state": state,
                    },
                )
            if "audio" in trace.subsystems:
                trace.record(
                    "audio",
                    {
                        "frame": frame,
                        "timestamp_ms": timestamp,
                        "kind": "audio_placeholder",
                        "available": False,
                        "reason": "PPSSPP debugger audio trace hook not exposed",
                    },
                )
            if "memory" in trace.subsystems:
                trace.record(
                    "memory",
                    {
                        "frame": frame,
                        "timestamp_ms": timestamp,
                        "kind": "memory_placeholder",
                        "available": False,
                        "reason": "No memory region list was supplied for this trace",
                    },
                )
        except Exception as exc:
            LOG.warning("trace probe failed run_id=%s error=%s", trace.run_id, exc)

    def press_button(
        self,
        button: str,
        *,
        pressed: bool | None = None,
        duration: int | None = None,
    ) -> dict[str, Any]:
        frame = self.current_frame()
        result = self.client.press_button(button, pressed=pressed, duration=duration)
        if duration is not None:
            self.recorder.append(frame, result["button"], "pulse", duration=duration)
        else:
            self.recorder.append(
                frame,
                result["button"],
                "press" if result.get("pressed") else "release",
            )
        return result

    def step_frame(self, count: int) -> dict[str, Any]:
        result = self.client.step_frame(count)
        self.record_trace_probe()
        return result

    def frame_hash(self) -> dict[str, Any]:
        frame = self.client.get_frame()
        raw = base64.b64decode(frame["png_base64"])
        return {
            "sha256": sha256_bytes(raw),
            "bytes": len(raw),
            "width": frame.get("width"),
            "height": frame.get("height"),
            "isFramebuffer": frame.get("isFramebuffer", False),
        }

    def memory_region_hash(self, address: int, size: int) -> dict[str, Any]:
        memory = self.client.read_memory(address, size)
        raw = base64.b64decode(memory.get("base64", ""))
        return {
            "address": address,
            "size": size,
            "sha256": sha256_bytes(raw),
        }

    def state_fingerprint(self) -> dict[str, Any]:
        state = self.client.get_state()
        normalized = normalize_for_diff(state, mask_addresses=True)
        return {
            "sha256": sha256_json(normalized),
            "state": normalized,
        }

    def load_inputs(self, path: Path) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ToolValidationError(
                    "invalid_inputs",
                    f"Invalid JSONL at line {line_no}: {exc}",
                ) from exc
            validate_input_event(event, line_no=line_no)
            events.append(event)
        self.loaded_inputs = sorted(events, key=lambda e: int(e["frame"]))
        self.loaded_inputs_path = str(path)
        return {
            "path": str(path),
            "event_count": len(self.loaded_inputs),
            "first_frame": self.loaded_inputs[0]["frame"] if self.loaded_inputs else None,
            "last_frame": self.loaded_inputs[-1]["frame"] if self.loaded_inputs else None,
            "sha256": sha256_bytes(path.read_bytes()),
        }

    def replay_run(self, frames: int, *, start_frame: int = 0) -> dict[str, Any]:
        if not self.loaded_inputs:
            raise ToolValidationError(
                "missing_inputs",
                "No replay inputs are loaded",
                hint="Call replay_load_inputs first.",
            )
        hashes: list[dict[str, Any]] = []
        by_frame: dict[int, list[dict[str, Any]]] = {}
        for event in self.loaded_inputs:
            by_frame.setdefault(int(event["frame"]), []).append(event)

        for offset in range(frames):
            frame = start_frame + offset
            for event in by_frame.get(frame, []):
                self.apply_input_event(event)
            digest = self.frame_hash()
            hashes.append({"frame": frame, "sha256": digest["sha256"]})
            self.client.step_frame(1)
            self.record_trace_probe()
        return {
            "input_path": self.loaded_inputs_path,
            "start_frame": start_frame,
            "end_frame": start_frame + frames - 1 if frames else start_frame,
            "frames": frames,
            "frame_hashes": hashes,
            "settings_fingerprint": self.state_fingerprint()["sha256"],
        }

    def apply_input_event(self, event: dict[str, Any]) -> None:
        action = event["action"]
        button = event["button"]
        if action == "pulse":
            self.client.press_button(button, duration=int(event.get("duration", 1)))
        elif action == "press":
            self.client.press_button(button, pressed=True)
        elif action == "release":
            self.client.press_button(button, pressed=False)


def validate_input_event(event: Any, *, line_no: int) -> None:
    if not isinstance(event, dict):
        raise ToolValidationError("invalid_inputs", f"Input event line {line_no} is not an object")
    frame = event.get("frame")
    button = event.get("button")
    action = event.get("action")
    if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
        raise ToolValidationError("invalid_inputs", f"Input event line {line_no} has invalid frame")
    if not isinstance(button, str) or button not in BUTTON_NAMES:
        raise ToolValidationError("invalid_inputs", f"Input event line {line_no} has invalid button")
    if action not in {"press", "release", "pulse"}:
        raise ToolValidationError("invalid_inputs", f"Input event line {line_no} has invalid action")
    if action == "pulse":
        duration = event.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 1:
            raise ToolValidationError("invalid_inputs", f"Input event line {line_no} has invalid duration")


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolValidationError(
            "invalid_manifest",
            f"Invalid manifest JSON: {exc}",
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != TRACE_SCHEMA_VERSION:
        raise ToolValidationError(
            "invalid_manifest",
            f"Manifest must use schema {TRACE_SCHEMA_VERSION}",
        )
    return manifest


def read_trace_events(manifest: dict[str, Any], subsystem: str) -> list[dict[str, Any]]:
    files = manifest.get("files")
    if not isinstance(files, dict) or subsystem not in files:
        raise ToolValidationError(
            "invalid_argument",
            f"Manifest does not contain subsystem '{subsystem}'",
        )
    info = files[subsystem]
    if not isinstance(info, dict) or not isinstance(info.get("path"), str):
        raise ToolValidationError(
            "invalid_manifest",
            f"Manifest file entry for '{subsystem}' is invalid",
        )
    path = resolve_artifact_path(info["path"], must_exist=True)
    expected = info.get("sha256")
    actual = sha256_bytes(path.read_bytes())
    if expected and expected != actual:
        raise ToolValidationError(
            "integrity_error",
            f"Trace stream hash mismatch for '{subsystem}'",
            hint=f"Expected {expected}, got {actual}",
        )
    events: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ToolValidationError(
                "invalid_trace",
                f"Invalid trace JSONL for '{subsystem}' at line {line_no}: {exc}",
            ) from exc
        if isinstance(event, dict):
            events.append(event)
    return events


def first_field_diff(reference: Any, target: Any, prefix: str = "") -> dict[str, Any] | None:
    if type(reference) is not type(target):
        return {"path": prefix or "$", "reference": reference, "target": target}
    if isinstance(reference, dict):
        keys = sorted(set(reference) | set(target))
        for key in keys:
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in reference or key not in target:
                return {
                    "path": child,
                    "reference": reference.get(key),
                    "target": target.get(key),
                }
            diff = first_field_diff(reference[key], target[key], child)
            if diff:
                return diff
        return None
    if isinstance(reference, list):
        for i, (left, right) in enumerate(zip(reference, target)):
            diff = first_field_diff(left, right, f"{prefix}[{i}]")
            if diff:
                return diff
        if len(reference) != len(target):
            return {
                "path": f"{prefix}.length" if prefix else "length",
                "reference": len(reference),
                "target": len(target),
            }
        return None
    if reference != target:
        return {"path": prefix or "$", "reference": reference, "target": target}
    return None


def diff_trace_manifests(
    reference_path: Path,
    target_path: Path,
    subsystem: str,
    *,
    context: int = 2,
    mask_addresses: bool = False,
) -> dict[str, Any]:
    reference_manifest = load_manifest(reference_path)
    target_manifest = load_manifest(target_path)
    reference_events = read_trace_events(reference_manifest, subsystem)
    target_events = read_trace_events(target_manifest, subsystem)
    max_len = max(len(reference_events), len(target_events))

    for index in range(max_len):
        ref_event = reference_events[index] if index < len(reference_events) else None
        tgt_event = target_events[index] if index < len(target_events) else None
        normalized_ref = normalize_for_diff(ref_event, mask_addresses=mask_addresses)
        normalized_tgt = normalize_for_diff(tgt_event, mask_addresses=mask_addresses)
        if normalized_ref != normalized_tgt:
            start = max(0, index - context)
            end = min(max_len, index + context + 1)
            return {
                "diverged": True,
                "subsystem": subsystem,
                "event_index": index,
                "frame": (
                    ref_event.get("frame")
                    if isinstance(ref_event, dict)
                    else tgt_event.get("frame") if isinstance(tgt_event, dict) else None
                ),
                "field_diff": first_field_diff(normalized_ref, normalized_tgt),
                "context": {
                    "reference": reference_events[start:min(end, len(reference_events))],
                    "target": target_events[start:min(end, len(target_events))],
                },
                "source": {
                    "reference": str(reference_path),
                    "target": str(target_path),
                },
            }

    return {
        "diverged": False,
        "subsystem": subsystem,
        "event_count": len(reference_events),
        "source": {
            "reference": str(reference_path),
            "target": str(target_path),
        },
    }


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
        self._connect_count = 0

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
            self._connect_count += 1
        except Exception as exc:
            self.ws = None
            raise BackendUnavailableError(
                f"Failed to connect to PPSSPP debugger "
                f"at {self.ws_url}: {exc}"
            ) from exc

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        self._deferred_events.clear()

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def backend_status(self) -> dict[str, Any]:
        started = time.monotonic()
        try:
            self.connect()
            response = self.request("cpu.status", timeout=self.request_timeout)
            return {
                "connected": True,
                "ws_url": self.ws_url,
                "latency_ms": round((time.monotonic() - started) * 1000, 3),
                "connect_count": self._connect_count,
                "stepping": bool(response.get("stepping", False)),
                "pc": response.get("pc"),
                "ticks": response.get("ticks"),
            }
        except Exception as exc:
            self.close()
            return {
                "connected": False,
                "ws_url": self.ws_url,
                "latency_ms": round((time.monotonic() - started) * 1000, 3),
                "connect_count": self._connect_count,
                "error": structured_error(exc, default_code="backend_status"),
            }

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

        try:
            self._send(payload)
        except Exception:
            self.reconnect()
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

        try:
            self._send(payload)
        except Exception:
            self.reconnect()
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

        raise FrameStepTimeoutError("Timed out waiting for debugger stepping state")

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
                raise FrameStepTimeoutError("Timed out waiting for frame step")

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
        if size < 1 or size > MAX_MEMORY_READ_SIZE:
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
                raise RequestTimeoutError(
                    "Timed out waiting for debugger event"
                ) from exc
            raise

        if raw == "":
            raise RequestTimeoutError("Timed out waiting for debugger event")

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
                raise RequestTimeoutError("Timed out waiting for debugger event")

            try:
                event = self._recv_json(remaining)
            except RequestTimeoutError:
                if time.monotonic() >= deadline:
                    raise RequestTimeoutError(
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


def jsonrpc_error(
    req_id: Any,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
    }
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": error,
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
        {
            "name": "backend_status",
            "description": "Check PPSSPP debugger websocket health.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "record_inputs_start",
            "description": "Start recording button events with frame indexes.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "record_inputs_stop",
            "description": "Stop the active input recording.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "record_inputs_export",
            "description": "Export recorded inputs to JSONL under the artifact root.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "output_dir": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "replay_load_inputs",
            "description": "Load a recorded input JSONL file for replay.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "replay_run",
            "description": "Replay loaded inputs from an optional savestate slot and return frame hashes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "frames": {"type": "integer", "minimum": 1, "maximum": MAX_REPLAY_FRAMES},
                    "start_frame": {"type": "integer", "minimum": 0, "default": 0},
                    "savestate_slot": {"type": "integer", "minimum": 0, "maximum": 4},
                },
                "required": ["frames"],
                "additionalProperties": False,
            },
        },
        {
            "name": "trace_start",
            "description": "Start a trace session writing JSONL streams under the artifact root.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subsystems": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["ge", "audio", "memory", "state", "frame_hash"]},
                        "default": ["ge", "state"],
                    },
                    "start_frame": {"type": "integer", "minimum": 0},
                    "max_frames": {"type": "integer", "minimum": 1, "maximum": MAX_TRACE_FRAMES},
                    "output_dir": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "trace_stop",
            "description": "Stop the active trace session.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "trace_export_manifest",
            "description": "Write and return a trace manifest with per-stream SHA-256 hashes.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "trace_summary",
            "description": "Return the active or most recent trace summary.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "frame_hash",
            "description": "Capture the current frame and return a SHA-256 hash.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "memory_region_hash",
            "description": "Hash a bounded PSP memory region.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "address": {"type": "integer"},
                    "size": {"type": "integer", "minimum": 1, "maximum": MAX_MEMORY_READ_SIZE},
                },
                "required": ["address", "size"],
                "additionalProperties": False,
            },
        },
        {
            "name": "state_fingerprint",
            "description": "Return a normalized hash of CPU/game state.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "diff_trace",
            "description": "Diff two trace manifests for a subsystem.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reference": {"type": "string"},
                    "target": {"type": "string"},
                    "subsystem": {"type": "string", "enum": ["ge", "audio", "memory", "state", "frame_hash"]},
                    "context": {"type": "integer", "minimum": 0, "maximum": 20, "default": 2},
                    "mask_addresses": {"type": "boolean", "default": False},
                },
                "required": ["reference", "target", "subsystem"],
                "additionalProperties": False,
            },
        },
        {
            "name": "find_first_divergence",
            "description": "Return the first deterministic divergence between two trace manifests.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reference": {"type": "string"},
                    "target": {"type": "string"},
                    "subsystem": {"type": "string", "enum": ["ge", "audio", "memory", "state", "frame_hash"]},
                    "context": {"type": "integer", "minimum": 0, "maximum": 20, "default": 2},
                    "mask_addresses": {"type": "boolean", "default": False},
                },
                "required": ["reference", "target", "subsystem"],
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


def resource_definitions() -> list[dict[str, Any]]:
    return [
        {
            "uri": "ppsspp://capabilities",
            "name": "PPSSPP MCP capabilities",
            "mimeType": "application/json",
        },
        {
            "uri": "ppsspp://session",
            "name": "PPSSPP MCP adapter session",
            "mimeType": "application/json",
        },
        {
            "uri": "ppsspp://game",
            "name": "Current PPSSPP game status",
            "mimeType": "application/json",
        },
        {
            "uri": "ppsspp://memory-map",
            "name": "PPSSPP memory map context",
            "mimeType": "application/json",
        },
        {
            "uri": "ppsspp://trace-schema",
            "name": "PPSSPP trace schema",
            "mimeType": "application/json",
        },
    ]


def read_resource(session: AdapterSession, uri: str) -> dict[str, Any]:
    if uri == "ppsspp://capabilities":
        data = {
            "tools": [tool["name"] for tool in tool_definitions()],
            "resources": [resource["uri"] for resource in resource_definitions()],
            "limits": {
                "max_memory_read_size": MAX_MEMORY_READ_SIZE,
                "max_frame_step_count": MAX_FRAME_STEP_COUNT,
                "max_replay_frames": MAX_REPLAY_FRAMES,
                "max_trace_frames": MAX_TRACE_FRAMES,
                "artifact_root": str(DEFAULT_ARTIFACT_ROOT.resolve()),
            },
            "buttons": sorted(BUTTON_NAMES),
        }
    elif uri == "ppsspp://session":
        data = {
            "ws_url": session.client.ws_url,
            "backend": session.client.backend_status(),
            "recording": {
                "active": session.recorder.active,
                "run_id": session.recorder.run_id,
                "events": len(session.recorder.events),
            },
            "trace": session.trace.summary() if session.trace else None,
            "loaded_inputs": {
                "path": session.loaded_inputs_path,
                "events": len(session.loaded_inputs),
            },
        }
    elif uri == "ppsspp://game":
        try:
            data = session.client.get_state().get("game", {})
        except Exception as exc:
            data = {"error": structured_error(exc)}
    elif uri == "ppsspp://memory-map":
        data = {
            "regions": [
                {
                    "name": "PSP RAM",
                    "start": "0x08000000",
                    "end": "0x0A000000",
                    "notes": "Common user RAM range; exact mapping depends on game and PPSSPP build.",
                }
            ],
            "max_read_size": MAX_MEMORY_READ_SIZE,
        }
    elif uri == "ppsspp://trace-schema":
        data = {
            "schema": TRACE_SCHEMA_VERSION,
            "streams": ["ge", "audio", "memory", "state", "frame_hash"],
            "event_common_fields": ["frame", "kind", "timestamp_ms"],
            "manifest_fields": [
                "schema",
                "run_id",
                "game_id",
                "build_id",
                "ppsspp_version",
                "settings_fingerprint",
                "start_frame",
                "end_frame",
                "files",
            ],
            "normalization": {
                "unstable_keys": ["timestamp", "timestamp_ms", "time", "host_path", "path", "run_id", "ticket"],
                "mask_addresses_option": True,
            },
        }
    else:
        raise ToolValidationError("not_found", f"Unknown resource URI: {uri}")

    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(data, ensure_ascii=False),
            }
        ]
    }


def call_tool(
    session: AdapterSession,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    client = session.client
    if name == "get_frame":
        require_no_extra(arguments, {"alpha"})
        frame = client.get_frame(alpha=require_bool(arguments, "alpha", default=False))
        return image_tool_result(frame)

    if name == "step_frame":
        require_no_extra(arguments, {"count"})
        count = require_int(arguments, "count", default=1, minimum=1, maximum=MAX_FRAME_STEP_COUNT)
        result = session.step_frame(count)
        return text_tool_result(result)

    if name == "press_button":
        require_no_extra(arguments, {"button", "pressed", "duration"})
        button = require_str(arguments, "button").strip().lower()
        if button not in BUTTON_NAMES:
            raise ToolValidationError(
                "invalid_argument",
                f"Unsupported button '{button}'",
                hint=f"Allowed buttons: {', '.join(sorted(BUTTON_NAMES))}",
            )
        duration_int = (
            require_int(arguments, "duration", minimum=1, maximum=MAX_FRAME_STEP_COUNT)
            if "duration" in arguments
            else None
        )
        pressed_bool = (
            require_bool(arguments, "pressed")
            if "pressed" in arguments
            else None
        )
        result = session.press_button(
            button,
            pressed=pressed_bool,
            duration=duration_int,
        )
        return text_tool_result(result)

    if name == "get_state":
        require_no_extra(arguments, set())
        return text_tool_result(client.get_state())

    if name == "read_memory":
        require_no_extra(arguments, {"address", "size"})
        result = client.read_memory(
            require_int(arguments, "address", minimum=0),
            require_int(arguments, "size", minimum=1, maximum=MAX_MEMORY_READ_SIZE),
        )
        return text_tool_result(result)

    if name == "reset_game":
        require_no_extra(arguments, set())
        return text_tool_result(client.reset_game())

    if name == "save_state":
        require_no_extra(arguments, {"slot"})
        slot = require_int(arguments, "slot", default=0, minimum=0, maximum=4)
        return text_tool_result(client.save_state(slot))

    if name == "load_state":
        require_no_extra(arguments, {"slot"})
        slot = require_int(arguments, "slot", default=0, minimum=0, maximum=4)
        return text_tool_result(client.load_state(slot))

    if name == "list_states":
        require_no_extra(arguments, set())
        return text_tool_result(client.list_states())

    if name == "backend_status":
        require_no_extra(arguments, set())
        return text_tool_result(client.backend_status())

    if name == "record_inputs_start":
        require_no_extra(arguments, set())
        return text_tool_result(session.recorder.start(session.current_frame()))

    if name == "record_inputs_stop":
        require_no_extra(arguments, set())
        return text_tool_result(session.recorder.stop(session.current_frame()))

    if name == "record_inputs_export":
        require_no_extra(arguments, {"output_dir"})
        output_dir = ensure_output_dir(arguments.get("output_dir"))
        return text_tool_result(session.recorder.export(output_dir))

    if name == "replay_load_inputs":
        require_no_extra(arguments, {"path"})
        path = resolve_artifact_path(require_str(arguments, "path"), must_exist=True)
        return text_tool_result(session.load_inputs(path))

    if name == "replay_run":
        require_no_extra(arguments, {"frames", "start_frame", "savestate_slot"})
        frames = require_int(arguments, "frames", minimum=1, maximum=MAX_REPLAY_FRAMES)
        start_frame = require_int(arguments, "start_frame", default=0, minimum=0)
        if "savestate_slot" in arguments:
            client.load_state(require_int(arguments, "savestate_slot", minimum=0, maximum=4))
        return text_tool_result(session.replay_run(frames, start_frame=start_frame))

    if name == "trace_start":
        require_no_extra(arguments, {"subsystems", "start_frame", "max_frames", "output_dir"})
        if session.trace is not None and session.trace.active:
            raise ToolValidationError("session_active", "A trace session is already active")
        subsystems = require_string_list(
            arguments,
            "subsystems",
            default=["ge", "state"],
            allowed={"ge", "audio", "memory", "state", "frame_hash"},
        )
        if not subsystems:
            raise ToolValidationError("invalid_argument", "At least one trace subsystem is required")
        start_frame = require_int(arguments, "start_frame", default=session.current_frame(), minimum=0)
        max_frames = require_int(arguments, "max_frames", default=600, minimum=1, maximum=MAX_TRACE_FRAMES)
        output_dir = ensure_output_dir(arguments.get("output_dir"))
        session.trace = TraceSession(
            subsystems=subsystems,
            start_frame=start_frame,
            max_frames=max_frames,
            output_dir=output_dir,
        )
        session.record_trace_probe()
        return text_tool_result(session.trace.summary())

    if name == "trace_stop":
        require_no_extra(arguments, set())
        if session.trace is None:
            raise ToolValidationError("session_inactive", "No trace session has been started")
        return text_tool_result(session.trace.stop(session.current_frame()))

    if name == "trace_summary":
        require_no_extra(arguments, set())
        if session.trace is None:
            raise ToolValidationError("session_inactive", "No trace session has been started")
        return text_tool_result(session.trace.summary())

    if name == "trace_export_manifest":
        require_no_extra(arguments, set())
        if session.trace is None:
            raise ToolValidationError("session_inactive", "No trace session has been started")
        try:
            game = client.get_state().get("game", {})
        except Exception:
            game = {}
        try:
            settings_fingerprint = session.state_fingerprint()["sha256"]
        except Exception as exc:
            settings_fingerprint = f"unavailable:{structured_error(exc)['code']}"
        manifest = session.trace.manifest(
            game=game,
            build_id=SERVER_VERSION,
            settings_fingerprint=settings_fingerprint,
        )
        manifest_path = Path(session.trace.output_dir) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return text_tool_result(
            {
                "manifest_path": str(manifest_path),
                "summary": session.trace.summary(),
                "manifest": manifest,
                "sha256": sha256_bytes(manifest_path.read_bytes()),
            }
        )

    if name == "frame_hash":
        require_no_extra(arguments, set())
        return text_tool_result(session.frame_hash())

    if name == "memory_region_hash":
        require_no_extra(arguments, {"address", "size"})
        address = require_int(arguments, "address", minimum=0)
        size = require_int(arguments, "size", minimum=1, maximum=MAX_MEMORY_READ_SIZE)
        return text_tool_result(session.memory_region_hash(address, size))

    if name == "state_fingerprint":
        require_no_extra(arguments, set())
        return text_tool_result(session.state_fingerprint())

    if name in {"diff_trace", "find_first_divergence"}:
        require_no_extra(arguments, {"reference", "target", "subsystem", "context", "mask_addresses"})
        subsystem = require_str(arguments, "subsystem").strip().lower()
        if subsystem not in {"ge", "audio", "memory", "state", "frame_hash"}:
            raise ToolValidationError("invalid_argument", f"Unknown subsystem '{subsystem}'")
        result = diff_trace_manifests(
            resolve_artifact_path(require_str(arguments, "reference"), must_exist=True),
            resolve_artifact_path(require_str(arguments, "target"), must_exist=True),
            subsystem,
            context=require_int(arguments, "context", default=2, minimum=0, maximum=20),
            mask_addresses=require_bool(arguments, "mask_addresses", default=False),
        )
        return text_tool_result(result)

    raise ToolValidationError("unknown_tool", f"Unknown tool '{name}'")


def run_server(
    ws_url: str,
    request_timeout: float,
    frame_timeout: float,
) -> int:
    client = PPSSPPDebuggerClient(ws_url, request_timeout, frame_timeout)
    session = AdapterSession(client)

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
                    "resources": {},
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
                result = call_tool(session, tool_name, args)
            except Exception as exc:
                LOG.warning("tool call failed tool=%s error=%s", tool_name, exc)
                result = text_tool_result(structured_error(exc), is_error=True)

            write_mcp_message(stdout_buffer, jsonrpc_response(req_id, result))
            continue

        if method == "resources/list":
            if req_id is not None:
                write_mcp_message(
                    stdout_buffer,
                    jsonrpc_response(req_id, {"resources": resource_definitions()}),
                )
            continue

        if method == "resources/read":
            if req_id is None:
                continue
            uri = params.get("uri")
            if not isinstance(uri, str):
                write_mcp_message(
                    stdout_buffer,
                    jsonrpc_error(
                        req_id,
                        -32602,
                        "Invalid params",
                        structured_error(
                            ToolValidationError(
                                "invalid_argument",
                                "Missing or invalid resource URI",
                            )
                        ),
                    ),
                )
                continue
            try:
                result = read_resource(session, uri)
            except Exception as exc:
                write_mcp_message(
                    stdout_buffer,
                    jsonrpc_error(
                        req_id,
                        -32602,
                        "Resource read error",
                        structured_error(exc),
                    ),
                )
                continue
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
    logging.basicConfig(
        level=os.environ.get("PPSSPP_MCP_LOG_LEVEL", "WARNING"),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args(argv)
    return run_server(args.ws_url, args.request_timeout, args.frame_timeout)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
