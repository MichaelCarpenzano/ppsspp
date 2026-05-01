# Developer-Agent Debugging with PPSSPP MCP

This note describes how to use PPSSPP as a target harness for AI-assisted PSP
software debugging. The goal is not to make the agent guess from screenshots or
source code alone. The goal is to give the agent comparable evidence: traces,
state snapshots, deterministic replay, and low-level diffs that reveal the first
point where behavior diverges.

## Recommended Architecture

Keep the MCP server as a separate adapter process, not embedded in PPSSPP.

For Codex, package that adapter as a plugin. The plugin should contain plugin
metadata, a `.mcp.json` entry that launches the adapter, and a skill that tells
Codex how to use the tools for emulator debugging. In other words: the plugin is
the distribution and workflow layer; MCP remains the tool protocol.

The preferred layering is:

1. PPSSPP exposes stable debugger primitives over its existing debugger
   WebSocket protocol.
2. The MCP adapter speaks MCP over stdio to developer agents.
3. The adapter translates MCP tool calls into PPSSPP debugger commands.
4. The Codex plugin packages the MCP server and debugger instructions.
5. Trace, normalize, and diff tools live outside the emulator unless they need
   a small target-side hook.

This keeps protocol churn and agent-specific behavior out of the emulator core.
It also lets non-MCP tools use the same PPSSPP debugger APIs.

Use stdio for the local MCP server. It is the common fit for a local developer
tool launched by an agent host. Consider Streamable HTTP only if the debugging
service needs to be shared across machines or long-lived independently from the
agent session.

## Codex Access

Codex can access the PPSSPP MCP adapter by registering it as a local stdio MCP
server:

```bash
codex mcp add ppsspp \
  --env PPSSPP_DEBUGGER_WS=ws://127.0.0.1:8765/debugger \
  -- /Library/Developer/CommandLineTools/usr/bin/python3 \
  /Users/michaelcarpenzano/Repos/ppsspp/Tools/mcp_adapter/ppsspp_mcp_server.py
```

Use the same debugger port that PPSSPP was launched with. The adapter defaults
to `3250`; the included `remote_debugger.ini` uses `8765`.

Useful Codex MCP commands:

```bash
codex mcp list
codex mcp get ppsspp
codex mcp remove ppsspp
```

After adding the server, restart Codex or open a new Codex session so the tool
surface is loaded. PPSSPP does not need to be running for Codex to list tools,
but it must be running with the debugger enabled before tool calls such as
`get_frame`, `step_frame`, or `read_memory` can succeed.

## Current Repo Fit

The current implementation follows this shape well:

- `ppsspp_mcp_server.py` is a thin stdio MCP adapter.
- PPSSPP remains the emulator and debugger target.
- New emulator hooks expose reusable debugger primitives:
  - `cpu.stepFrame`
  - GPU framebuffer capture while CPU stepping
  - input injection
  - memory reads
  - in-memory savestate slots
- The adapter exposes a small tool surface:
  - `get_frame`
  - `step_frame`
  - `press_button`
  - `get_state`
  - `read_memory`
  - `reset_game`
  - `save_state`
  - `load_state`
  - `list_states`

That is the right first layer. The next layer should be trace and diff tools
that help a developer agent isolate the first divergence.

## Debugging Principle

For visual, audio, or gameplay bugs, do not start by guessing from source.
Capture the same frame/input on the reference and PSP target, diff the lowest
comparable signal, and investigate the first divergence.

For rendering, compare command/state streams first and screenshots second.
For audio, compare generated blocks or waveforms before relying on listening.
For gameplay, compare deterministic state probes before stepping through broad
source regions.

## Deterministic Replay First

Deterministic replay should be the foundation for all higher-level tools.

Minimum replay features:

- Record controller input per frame.
- Support fixed seed control from the game or test harness.
- Include frame counters in every trace.
- Support scene jump or debug boot arguments when possible.
- Include enough metadata to reproduce the run:
  - game ID
  - executable/build hash
  - PPSSPP version
  - emulator settings that affect timing, CPU, GPU, or audio
  - initial savestate/checkpoint ID

Every trace event should include:

- frame number
- subsystem
- call index within the frame
- key state values
- source location when available
- target build identifier

## MCP Tool Design

Expose tools that are stable, narrow, and composable. Prefer facts over large
opaque blobs unless the blob is intentionally consumed by a diff tool.

Good MCP tools:

- `replay_start`
- `replay_stop`
- `replay_load_inputs`
- `replay_step`
- `trace_start`
- `trace_stop`
- `trace_export`
- `diff_trace`
- `get_frame`
- `get_state`
- `read_memory`
- `save_state`
- `load_state`

Avoid a single broad `debug_game` or `run_command` tool. Developer agents work
better when the tool list communicates safe, concrete operations.

Use MCP resources for read-only context:

- current game metadata
- debugger capability list
- memory map
- trace schema
- current emulator settings
- latest trace manifest

Use MCP tools for actions:

- stepping
- input injection
- saving/loading state
- starting/stopping traces
- exporting captures
- running diffs

## Suggested Tool Stack

The following tools should live under `Tools/` or `Tools/mcp_adapter/` depending
on whether they are general PPSSPP utilities or MCP-specific wrappers.

### GE Trace

Purpose: capture PSP GE command lists and `sceGu*` state per frame before
submission.

Suggested files:

- `Tools/ge_trace/psp_ge_trace.c`
- `Tools/ge_trace/ppsspp_ge_dump_notes.md`
- `Tools/ge_trace/schema.md`

Capture:

- frame number
- command list address/range
- primitive type
- vertex type and vertex count
- texture base, stride, format, dimensions
- CLUT loads
- matrices
- viewport and scissor
- blend, depth, alpha test state
- framebuffer and depthbuffer addresses
- source location when instrumented in the PSP build

MCP-facing tools:

- `ge_trace_start`
- `ge_trace_stop`
- `ge_trace_export`
- `ge_trace_summary`

### GE Diff

Purpose: compare decoded GE streams between reference and PSP builds, or between
known-good and broken revisions.

Suggested files:

- `Tools/ge_diff/ge_decode.py`
- `Tools/ge_diff/ge_diff.py`

Diff behavior:

- Normalize pointer-like addresses when appropriate.
- Diff command streams by frame and call index.
- Report the first divergent command/state field.
- Include compact context before and after the divergence.
- Link back to raw trace artifacts.

MCP-facing tools:

- `ge_diff`
- `ge_find_first_divergence`

### Audio Trace

Purpose: capture PSP audio calls, decoded sample blocks, voice state, and loop
metadata.

Suggested files:

- `Tools/audio_trace/psp_audio_trace.c`
- `Tools/audio_trace/schema.md`

Capture:

- `sceAudio*` calls
- `sceSasCore` voice state
- sample rate
- channel
- volume
- ADSR state
- decoded PCM block hashes
- ADPCM state
- loop points
- ring-buffer write positions

MCP-facing tools:

- `audio_trace_start`
- `audio_trace_stop`
- `audio_trace_export`

### Audio Diff

Purpose: compare audio evidence at waveform or block level.

Suggested files:

- `Tools/audio_diff/pcm_diff.py`
- `Tools/audio_diff/adpcm_decode_check.py`

Diff behavior:

- Compare PCM block hashes first.
- Fall back to sample-level diffs when hashes diverge.
- Report first divergent sample/block.
- Summarize amplitude, phase, channel, and loop-point differences.

MCP-facing tools:

- `audio_diff`
- `audio_find_first_divergence`

### Asset Extract Diff

Purpose: compare source assets, extracted original assets, and PSP-converted
assets.

Suggested files:

- `Tools/asset_diff/texture_dump.py`
- `Tools/asset_diff/table_hash.py`
- `Tools/asset_diff/model_hash.py`
- `Tools/asset_diff/palette_diff.py`

Compare:

- textures
- palettes/CLUTs
- models
- animations
- packed tables
- relocation-style blobs
- endian conversions
- swizzled/unswizzled texture data

MCP-facing tools:

- `asset_extract`
- `asset_diff`
- `asset_hash_manifest`

### Layout Probe

Purpose: compile PSP/MIPS probes and inspect ABI-sensitive layout facts.

Suggested files:

- `Tools/layout/layout_probe.c`
- `Tools/layout/parse_objdump.py`

Probe:

- `sizeof`
- `offsetof`
- bitfield layout
- alignment
- packing assumptions
- enum sizes
- endian-sensitive fields

Notes:

PSP is 32-bit MIPS little-endian, so LP64 pointer-width issues are avoided, but
alignment and bitfield layout can still bite.

MCP-facing tools:

- `layout_probe_build`
- `layout_probe_run`
- `layout_diff`

### Memory Trace

Purpose: capture allocator and data movement facts around the failing frame.

Suggested files:

- `Tools/memory_trace/alloc_trace.py`
- `Tools/memory_trace/vram_trace.py`
- `Tools/memory_trace/memcopy_trace.py`

Capture:

- allocator events
- asset load addresses
- DMA-like copies
- VRAM allocations
- stack/heap high-water marks
- suspicious overlapping copies

MCP-facing tools:

- `memory_trace_start`
- `memory_trace_stop`
- `memory_trace_export`
- `memory_region_hash`

## First MCP Extensions to Build

After the current basic adapter, the most useful developer-agent additions are:

1. `record_inputs`
2. `play_inputs`
3. `capture_trace`
4. `export_trace`
5. `diff_trace`
6. `find_first_divergence`
7. `memory_region_hash`
8. `ge_trace_summary`
9. `audio_trace_summary`
10. `artifact_manifest`

The important part is not the number of tools. The important part is that each
tool returns structured evidence the agent can reason about without having to
parse ad hoc logs.

## Artifact Format

Prefer a manifest plus separate payload files.

Example:

```json
{
  "schema": "ppsspp.trace.manifest.v1",
  "game_id": "ULUS00000",
  "build_id": "debug-abc123",
  "ppsspp_version": "vX.Y.Z",
  "start_frame": 120,
  "end_frame": 180,
  "inputs": "inputs.jsonl",
  "ge_trace": "ge_trace.jsonl",
  "audio_trace": "audio_trace.jsonl",
  "memory_trace": "memory_trace.jsonl",
  "frames": [
    {
      "frame": 120,
      "png": "frames/frame_00120.png",
      "sha256": "..."
    }
  ]
}
```

Use JSONL for event streams. It is easy to append, easy to diff, and easy for
agents to sample. Use binary payloads only when necessary, and reference them
from the manifest by path plus hash.

## Safety and Boundaries

The MCP server can read emulator memory, inject input, reset games, and load
savestates. Treat it as a powerful local debugging interface.

Recommended safeguards:

- Bind PPSSPP debugger access to localhost by default.
- Keep the MCP adapter local over stdio by default.
- Avoid arbitrary shell execution tools.
- Validate all MCP tool arguments server-side.
- Cap memory reads and trace export sizes.
- Return tool errors without leaking host paths unless the path is part of the
  requested artifact.
- Send logs to stderr or files, never stdout, because stdout carries MCP
  protocol frames.

## Implementation Notes for This Repo

Current adapter improvements to consider:

- Move from manual MCP JSON-RPC handling to an official MCP SDK when dependency
  cost is acceptable.
- Add explicit runtime argument validation in `call_tool`, not only advertised
  JSON schemas.
- Add resources for read-only state such as game metadata, debugger
  capabilities, memory map, and trace schemas.
- Add cleanup or a memory budget for in-memory savestate slots.
- Add a small test client that performs initialize, tools/list, and a mocked
  tools/call without requiring PPSSPP to be running.

The core split should stay as-is: PPSSPP provides debugger facts and control;
the MCP adapter packages those capabilities for developer agents.
