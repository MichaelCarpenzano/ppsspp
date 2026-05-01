# PPSSPP MCP Adapter

This folder contains a thin MCP server that bridges to PPSSPP's built-in debugger websocket.

## Capabilities

- `get_frame` (PNG image data from current framebuffer)
- `step_frame` (deterministic frame advance)
- `press_button` (input injection)
- `get_state` (CPU/game status)
- `read_memory` (raw PSP RAM read, returns base64)
- `reset_game` (reboot the running game)
- `save_state` (save to in-memory savestate slot 0-4)
- `load_state` (load from in-memory savestate slot 0-4)
- `list_states` (list in-memory savestate slot occupancy)

## Transport

- MCP server transport: stdio (JSON-RPC with `Content-Length` framing)
- Emulator transport: websocket `ws://<host>:<port>/debugger`
- Websocket subprotocol: `debugger.ppsspp.org`

## Prerequisites

1. PPSSPP running with debugger web server enabled.
2. Python 3.10+.
3. Install dependency:

   - `pip install -r Tools/mcp_adapter/requirements.txt`

## Run

- `python3 Tools/mcp_adapter/ppsspp_mcp_server.py`

Optional overrides:

- `PPSSPP_DEBUGGER_WS` (default: `ws://127.0.0.1:3250/debugger`)
- `PPSSPP_DEBUGGER_TIMEOUT` (default: `6.0` seconds)
- `PPSSPP_DEBUGGER_FRAME_TIMEOUT` (default: `15.0` seconds)

You can also pass CLI flags:

- `--ws-url`
- `--request-timeout`
- `--frame-timeout`

## Use from Codex

Codex can launch this adapter as a local stdio MCP server. Register it once
with the Codex MCP manager:

```bash
codex mcp add ppsspp \
  --env PPSSPP_DEBUGGER_WS=ws://127.0.0.1:8765/debugger \
  -- /Library/Developer/CommandLineTools/usr/bin/python3 \
  /Users/michaelcarpenzano/Repos/ppsspp/Tools/mcp_adapter/ppsspp_mcp_server.py
```

Use the same port that PPSSPP's debugger is listening on. The adapter's built-in
default is `ws://127.0.0.1:3250/debugger`, while this directory's
`remote_debugger.ini` uses port `8765`.

Check the registration:

```bash
codex mcp list
codex mcp get ppsspp
```

Then restart Codex or open a new Codex session so the new MCP tools are loaded.
PPSSPP only needs to be running when a tool is called; `tools/list` works
without an active emulator connection.

If you use a virtual environment, point Codex at that Python instead:

```bash
python3 -m venv Tools/mcp_adapter/.venv
Tools/mcp_adapter/.venv/bin/python -m pip install -r Tools/mcp_adapter/requirements.txt
codex mcp add ppsspp \
  --env PPSSPP_DEBUGGER_WS=ws://127.0.0.1:8765/debugger \
  -- /Users/michaelcarpenzano/Repos/ppsspp/Tools/mcp_adapter/.venv/bin/python \
  /Users/michaelcarpenzano/Repos/ppsspp/Tools/mcp_adapter/ppsspp_mcp_server.py
```

Remove the registration with:

```bash
codex mcp remove ppsspp
```

## Use as a Codex Plugin

This repo also includes a repo-local Codex plugin at:

- `plugins/ppsspp-debugger`

The plugin is packaging around this MCP adapter. It provides:

- `.codex-plugin/plugin.json` for Codex plugin metadata
- `.mcp.json` for the bundled PPSSPP MCP server
- `skills/ppsspp-debugger/SKILL.md` with debugger workflow instructions
- `scripts/run_ppsspp_mcp.py` to launch the repo-local adapter

This is usually better than registering the raw MCP server when the goal is a
reusable Codex workflow, because the plugin can bundle both tools and operating
instructions. It is not a replacement for MCP; it uses MCP as the tool
transport.

## Agent Loop Utility

A minimal observe→act helper is included:

- `python3 Tools/mcp_adapter/agent_loop.py --ws-url ws://127.0.0.1:8765/debugger --iterations 50`

It performs per-iteration frame capture, simple action policy, frame stepping,
and JSONL logging for rapid integration testing.

## Savestate Round-Trip Validator

- `python3 Tools/mcp_adapter/validate_savestate_roundtrip.py --ws-url ws://127.0.0.1:8765/debugger --slot 0`

This validates save/load restoration by checking CPU `pc` + `ticks` before save
and after load.

## Step Latency Benchmark

- `python3 Tools/mcp_adapter/benchmark_step_latency.py --ws-url ws://127.0.0.1:8765/debugger --iterations 50`

Reports `step_frame(1)` round-trip latency stats (`mean`, `p50`, `p95`, etc.)
for performance tuning.

## Notes

- `step_frame` automatically ensures CPU stepping mode before advancing.
- `get_frame` returns MCP image content with PNG base64 data.
- `save_state` / `load_state` require stepping mode and enforce slot bounds.
- If PPSSPP is not reachable, tool calls return MCP tool errors.
