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

    argv = [sys.executable, str(server), *sys.argv[1:]]
    os.execvpe(sys.executable, argv, os.environ.copy())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
