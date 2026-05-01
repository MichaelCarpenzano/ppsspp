---
name: "ppsspp-debugger"
description: "Use when debugging PSP software in PPSSPP, interacting with the PPSSPP debugger MCP server, capturing frames, stepping emulation, injecting input, reading PSP memory, or managing in-memory savestates."
---

# PPSSPP Debugger

Use this plugin when Codex needs to debug or inspect PSP software running in
PPSSPP.

## Requirements

- PPSSPP must be running with the debugger WebSocket enabled.
- The default plugin connection is `ws://127.0.0.1:8765/debugger`.
- Use `Tools/mcp_adapter/remote_debugger.ini` or an equivalent
  `--appendconfig` file when launching PPSSPP.

## Workflow

1. Confirm the user has PPSSPP running with the target game loaded.
2. Use MCP tools for emulator interaction instead of guessing from screenshots
   or source alone.
3. Prefer deterministic evidence:
   - capture frame and state
   - save a state before experiments
   - apply one input/action
   - step a fixed number of frames
   - compare frame/state/memory results
4. For visual, audio, or gameplay bugs, seek the first divergence between a
   reference run and the PSP/PPSSPP run.

## Tool Use

Expected MCP tools include:

- `get_frame`
- `step_frame`
- `press_button`
- `get_state`
- `read_memory`
- `reset_game`
- `save_state`
- `load_state`
- `list_states`

Call `get_state` first when checking connectivity. If PPSSPP is not reachable,
tell the user to launch PPSSPP with the debugger enabled and the configured
port.

## Safety

Treat this as a local debugger with powerful control over the emulator. Avoid
large memory reads unless needed, and do not repeatedly load/reset state without
explaining the impact to the user.
