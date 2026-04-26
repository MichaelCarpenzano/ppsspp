#!/usr/bin/env python3
"""Validate deterministic frame output across two fresh PPSSPP runs.

Usage:
  python3 test_determinism.py \
    --iso /path/to/EBOOT.PBP \
    --iterations 30 \
    --action-every 5
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time

from ppsspp_mcp_server import PPSSPPDebuggerClient

PPSSPP_BIN = os.path.join(
    os.path.dirname(__file__),
    "../../build/PPSSPPSDL.app/Contents/MacOS/PPSSPPSDL",
)


def create_append_config(port: int) -> str:
    fd, path = tempfile.mkstemp(
        prefix="ppsspp_remote_debugger_",
        suffix=".ini",
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("[General]\n")
        f.write(f"RemoteISOPort = {port}\n")
        f.write("RemoteDebuggerOnStartup = True\n")
        f.write("RemoteDebuggerLocal = True\n")
    return path


def launch_ppsspp(iso: str, port: int = 8765) -> tuple[subprocess.Popen, str]:
    config_path = create_append_config(port)
    proc = subprocess.Popen(
        [os.path.realpath(PPSSPP_BIN), f"--appendconfig={config_path}", iso],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, config_path


def wait_for_debugger(ws_url: str, timeout: int = 20) -> bool:
    import websocket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ws = websocket.create_connection(
                ws_url,
                subprotocols=["debugger.ppsspp.org"],
                timeout=3,
            )
            ws.send(json.dumps({"event": "version", "ticket": "t0"}))
            ws.recv()
            ws.close()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def run_sequence(
    ws_url: str,
    iterations: int,
    button: str,
    action_every: int,
) -> list[str]:
    """Run one deterministic sequence, returning frame SHA-256 hashes."""
    client = PPSSPPDebuggerClient(
        ws_url=ws_url,
        request_timeout=10.0,
        frame_timeout=15.0,
    )
    client.connect()
    client.ensure_stepping()
    hashes: list[str] = []
    for i in range(iterations):
        frame = client.get_frame()
        raw = base64.b64decode(frame["png_base64"])
        hashes.append(hashlib.sha256(raw).hexdigest())
        if i % action_every == 0:
            client.press_button(button, duration=1)
        client.step_frame(1)
    client.close()
    return hashes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--iso",
        default="/Users/michaelcarpenzano/Documents/PSP_ISO/pong/EBOOT.PBP",
    )
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8765/debugger")
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--action-every", type=int, default=5)
    parser.add_argument("--button", default="cross")
    parser.add_argument(
        "--boot-wait",
        type=int,
        default=10,
        help="Seconds to wait after PPSSPP launch before connecting",
    )
    args = parser.parse_args()

    results: dict[str, list[str]] = {}
    for run_id in ("A", "B"):
        print(f"\n=== Run {run_id} ===")
        proc, config_path = launch_ppsspp(args.iso)
        try:
            print(f"  Launched PPSSPP pid={proc.pid},")
            print(f"  waiting {args.boot_wait}s...")
            time.sleep(args.boot_wait)
            if not wait_for_debugger(args.ws_url):
                print("  ERROR: debugger did not become ready")
                proc.terminate()
                return 1
            print("  Debugger ready. Running sequence...")
            hashes = run_sequence(
                args.ws_url,
                args.iterations,
                args.button,
                args.action_every,
            )
            results[run_id] = hashes
            print(
                f"  Run {run_id}: {len(hashes)} hashes, "
                f"unique={len(set(hashes))}"
            )
            print(f"  First: {hashes[0][:16]}  Last: {hashes[-1][:16]}")
        finally:
            proc.terminate()
            proc.wait()
            try:
                os.unlink(config_path)
            except OSError:
                pass
            time.sleep(2)

    print("\n=== Determinism Result ===")
    matches = sum(a == b for a, b in zip(results["A"], results["B"]))
    total = len(results["A"])
    print(f"  Matching frames: {matches}/{total}")
    if matches == total:
        print("  PASS: Both runs produced identical frame sequences.")
        return 0

    mismatches = [
        (i, results["A"][i][:16], results["B"][i][:16])
        for i in range(total)
        if results["A"][i] != results["B"][i]
    ]
    print(f"  FAIL: {total - matches} mismatches:")
    for idx, ha, hb in mismatches[:10]:
        print(f"    iter {idx}: A={ha} B={hb}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
