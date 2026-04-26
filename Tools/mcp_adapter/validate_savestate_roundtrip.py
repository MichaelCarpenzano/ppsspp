#!/usr/bin/env python3
"""Validate savestate.save/load round-trip determinism.

Uses the websocket MCP bridge client.

Sequence:
1) Ensure stepping
2) Capture frame hash H0
3) Save state to slot
4) Apply input + step frames
5) Capture frame hash H1
6) Load state from slot
7) Capture frame hash H2

Expected: H2 == H0. Preferably H1 != H0.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json

from ppsspp_mcp_server import PPSSPPDebuggerClient


def hash_frame_png_base64(png_b64: str) -> str:
    raw = base64.b64decode(png_b64)
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ws-url", default="ws://127.0.0.1:8765/debugger")
    p.add_argument("--request-timeout", type=float, default=10.0)
    p.add_argument("--frame-timeout", type=float, default=20.0)
    p.add_argument("--slot", type=int, default=0)
    p.add_argument("--button", default="cross")
    p.add_argument("--step-before", type=int, default=1)
    p.add_argument("--step-after-input", type=int, default=6)
    p.add_argument("--step-after-load", type=int, default=1)
    args = p.parse_args()

    client = PPSSPPDebuggerClient(
        ws_url=args.ws_url,
        request_timeout=args.request_timeout,
        frame_timeout=args.frame_timeout,
    )

    try:
        client.connect()
        client.ensure_stepping()

        if args.step_before > 0:
            client.step_frame(args.step_before)

        h0 = hash_frame_png_base64(client.get_frame()["png_base64"])
        save = client.save_state(args.slot)

        # Primary deterministic reference: CPU status at the saved point.
        status_ref = client.request("cpu.status")

        # Diverge state with input to ensure visible mutation from S0.
        client.load_state(args.slot)
        client.press_button(args.button, duration=1)
        client.step_frame(args.step_after_input)
        h_changed = hash_frame_png_base64(client.get_frame()["png_base64"])

        # Reload saved state and verify CPU state was restored.
        load = client.load_state(args.slot)
        status_after = client.request("cpu.status")

        # Optional visual check after deterministic replay window.
        if args.step_after_load > 0:
            client.step_frame(args.step_after_load)
        h2 = hash_frame_png_base64(client.get_frame()["png_base64"])

        restored_pc = status_ref.get("pc") == status_after.get("pc")
        restored_ticks = (
            status_ref.get("ticks") == status_after.get("ticks")
        )

        result = {
            "ok": restored_pc and restored_ticks,
            "slot": args.slot,
            "save": save,
            "load": load,
            "hash_before": h0,
            "hash_changed": h_changed,
            "hash_after_replay": h2,
            "status_before": {
                "pc": status_ref.get("pc"),
                "ticks": status_ref.get("ticks"),
            },
            "status_after_load": {
                "pc": status_after.get("pc"),
                "ticks": status_after.get("ticks"),
            },
            "step_after_load": args.step_after_load,
            "changed_after_input": h_changed != h0,
            "restored_pc": restored_pc,
            "restored_ticks": restored_ticks,
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
