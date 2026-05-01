#!/usr/bin/env python3
"""Launch the repo-local PPSSPP MCP adapter from the Codex plugin."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def main() -> int:
    repo_root = Path(__file__).resolve().parents[3]
    server = repo_root / "Tools" / "mcp_adapter" / "ppsspp_mcp_server.py"
    if not server.is_file():
        print(f"PPSSPP MCP server not found: {server}", file=sys.stderr)
        return 1

    try:
        import websocket  # noqa: F401
    except Exception:
        print(
            "Missing Python dependency 'websocket-client'. Install with: "
            "python3 -m pip install -r Tools/mcp_adapter/requirements.txt. "
            "Starting anyway so MCP initialization and tool/resource listing "
            "can still report adapter capabilities.",
            file=sys.stderr,
        )

    ws_url = os.environ.get("PPSSPP_DEBUGGER_WS", "ws://127.0.0.1:3250/debugger")
    if ":3250/" in ws_url:
        print(
            "PPSSPP_DEBUGGER_WS is using the adapter default port 3250. "
            "If you launched PPSSPP with Tools/mcp_adapter/remote_debugger.ini, "
            "use ws://127.0.0.1:8765/debugger instead.",
            file=sys.stderr,
        )

    argv = [sys.executable, str(server), *sys.argv[1:]]
    os.execvpe(sys.executable, argv, os.environ.copy())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
