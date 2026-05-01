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
2. Call `backend_status` first. If it is not connected, check the configured
   `PPSSPP_DEBUGGER_WS` value and the debugger port (`3250` adapter default,
   `8765` in `Tools/mcp_adapter/remote_debugger.ini`).
3. Use MCP resources for read-only context before taking actions:
   `ppsspp://capabilities`, `ppsspp://session`, `ppsspp://game`,
   `ppsspp://memory-map`, and `ppsspp://trace-schema`.
4. Prefer deterministic evidence:
   - save a state before experiments
   - `record_inputs_start`
   - apply inputs with `press_button`
   - step a fixed frame count
   - `record_inputs_stop`
   - `record_inputs_export`
   - replay with `replay_load_inputs` and `replay_run`, passing
     `savestate_slot` when reproducing from a known checkpoint
   - compare `frame_hash`, `memory_region_hash`, and `state_fingerprint`
5. For visual, audio, memory, or gameplay bugs, capture traces:
   - `trace_start` with the smallest useful subsystem set
   - reproduce the issue
   - `trace_stop`
   - `trace_export_manifest`
   - compare reference and target manifests with `find_first_divergence`

## Tool Use

Expected MCP tools include:

- `backend_status`
- `get_frame`
- `step_frame`
- `press_button`
- `get_state`
- `read_memory`
- `reset_game`
- `save_state`
- `load_state`
- `list_states`
- `record_inputs_start`
- `record_inputs_stop`
- `record_inputs_export`
- `replay_load_inputs`
- `replay_run`
- `trace_start`
- `trace_stop`
- `trace_export_manifest`
- `trace_summary`
- `frame_hash`
- `memory_region_hash`
- `state_fingerprint`
- `diff_trace`
- `find_first_divergence`

Invalid arguments return structured tool errors with `code`, `message`, `hint`,
and `retryable`. Use those fields when deciding whether to retry, reduce a
limit, or ask the user to fix PPSSPP startup.

## Safety

Treat this as a local debugger with powerful control over the emulator. Avoid
large memory reads unless needed, and do not repeatedly load/reset state without
explaining the impact to the user. Keep trace sessions bounded with
`max_frames`, write artifacts only under the configured artifact root, and avoid
exporting more subsystems than the bug requires.
