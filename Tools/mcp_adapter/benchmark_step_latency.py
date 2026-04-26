#!/usr/bin/env python3
"""Benchmark `step_frame(1)` round-trip latency against PPSSPP debugger."""

from __future__ import annotations

import argparse
import json
import statistics
import time

from ppsspp_mcp_server import PPSSPPDebuggerClient


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values_sorted = sorted(values)
    idx = int((len(values_sorted) - 1) * p)
    return values_sorted[idx]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8765/debugger")
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--frame-timeout", type=float, default=20.0)
    parser.add_argument("--iterations", type=int, default=50)
    args = parser.parse_args()

    client = PPSSPPDebuggerClient(
        ws_url=args.ws_url,
        request_timeout=args.request_timeout,
        frame_timeout=args.frame_timeout,
    )

    timings_ms: list[float] = []
    try:
        client.connect()
        client.ensure_stepping()
        for _ in range(args.iterations):
            t0 = time.perf_counter()
            client.step_frame(1)
            dt_ms = (time.perf_counter() - t0) * 1000.0
            timings_ms.append(dt_ms)

        result = {
            "ok": True,
            "iterations": len(timings_ms),
            "mean_ms": round(statistics.mean(timings_ms), 3),
            "min_ms": round(min(timings_ms), 3),
            "max_ms": round(max(timings_ms), 3),
            "p50_ms": round(percentile(timings_ms, 0.50), 3),
            "p95_ms": round(percentile(timings_ms, 0.95), 3),
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
