#!/usr/bin/env python3
"""Minimal observe→act loop for PPSSPP MCP debugging workflows.

This utility uses the existing websocket client directly to:
1) observe current frame/state,
2) choose a simple action,
3) send input,
4) step one or more frames,
5) log each iteration.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import time
from pathlib import Path

from ppsspp_mcp_server import PPSSPPDebuggerClient


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run a simple PPSSPP observe-act loop"
    )
    p.add_argument("--ws-url", default="ws://127.0.0.1:3250/debugger")
    p.add_argument("--request-timeout", type=float, default=6.0)
    p.add_argument("--frame-timeout", type=float, default=15.0)
    p.add_argument("--iterations", type=int, default=20)
    p.add_argument("--step-count", type=int, default=1)
    p.add_argument(
        "--button",
        default="cross",
        help="Button to pulse on action frames",
    )
    p.add_argument(
        "--action-every",
        type=int,
        default=5,
        help="Pulse button every N iterations",
    )
    p.add_argument("--output-dir", default="Tools/mcp_adapter/loop_logs")
    p.add_argument(
        "--save-frame-every",
        type=int,
        default=0,
        help="Save PNG every N iterations (0 disables)",
    )
    return p.parse_args()


def choose_action(
    i: int, button: str, action_every: int
) -> dict[str, object] | None:
    if action_every > 0 and i % action_every == 0:
        return {"type": "press", "button": button, "duration": 1}
    return None


def main() -> int:
    args = parse_args()
    if args.iterations < 1:
        raise SystemExit("--iterations must be >= 1")
    if args.step_count < 1:
        raise SystemExit("--step-count must be >= 1")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"loop_{int(time.time())}.jsonl"

    client = PPSSPPDebuggerClient(
        ws_url=args.ws_url,
        request_timeout=args.request_timeout,
        frame_timeout=args.frame_timeout,
    )

    try:
        client.get_state()

        with log_path.open("w", encoding="utf-8") as logf:
            for i in range(args.iterations):
                frame = client.get_frame()
                raw = base64.b64decode(frame["png_base64"])
                frame_hash = hashlib.sha256(raw).hexdigest()

                action = choose_action(i, args.button, args.action_every)
                action_result: dict[str, object] | None = None
                if action and action["type"] == "press":
                    action_result = client.press_button(
                        str(action["button"]),
                        duration=int(action["duration"]),
                    )

                step = client.step_frame(args.step_count)

                if (
                    args.save_frame_every > 0
                    and i % args.save_frame_every == 0
                ):
                    (out_dir / f"frame_{i:05d}.png").write_bytes(raw)

                record = {
                    "iteration": i,
                    "timestamp": time.time(),
                    "frame": {
                        "width": frame.get("width"),
                        "height": frame.get("height"),
                        "sha256": frame_hash,
                    },
                    "action": action,
                    "action_result": action_result,
                    "step": step,
                }
                logf.write(json.dumps(record, ensure_ascii=False) + "\n")

        print(
            json.dumps(
                {"ok": True, "log": str(log_path)},
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
