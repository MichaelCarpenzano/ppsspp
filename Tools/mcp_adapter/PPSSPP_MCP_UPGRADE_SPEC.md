# PPSSPP-MCP Upgrade Spec

This document defines the concrete changes needed to move the current
`Tools/mcp_adapter/ppsspp_mcp_server.py` implementation to the target spec we
agreed on:

- Keep MCP as a separate adapter process (not embedded into core emulator app).
- Package it as a Codex plugin for repeatable developer-agent workflows.
- Shift debugging from guesswork to deterministic evidence:
  trace -> normalize -> diff -> first divergence.
- Keep a narrow, stable tool surface with strong validation and clear safety.

---

## 1. Current Baseline

The server already provides:

- `get_frame`
- `step_frame`
- `press_button`
- `get_state`
- `read_memory`
- `reset_game`
- `save_state`
- `load_state`
- `list_states`

Transport:

- MCP over stdio (`Content-Length` framing)
- PPSSPP debugger WebSocket backend (`debugger.ppsspp.org`)

This is a strong foundation. The work below upgrades it into a robust
developer-agent debugging platform.

---

## 2. Target Outcomes

1. Deterministic, replayable bug reproduction.
2. Structured evidence artifacts (manifest + JSONL streams + hashes).
3. First-divergence analysis tools for graphics/audio/state.
4. Clear separation of read-only context (`resources`) vs actions (`tools`).
5. Reliable Codex plugin packaging around the MCP adapter.
6. Strong argument validation, limits, and operational safety.

---

## 3. Required Server Changes

## 3.1 Add Deterministic Replay Controls

Implement MCP tools:

- `record_inputs_start`
- `record_inputs_stop`
- `record_inputs_export`
- `replay_load_inputs`
- `replay_run`

Behavior:

- Record button events with frame index.
- Replay inputs deterministically from a known savestate/checkpoint.
- Return run metadata (frame range, hashes, settings fingerprint).

Data format:

- Inputs stored as JSONL:
  - `frame`
  - `button`
  - `action` (`press`, `release`, `pulse`)
  - `duration` when applicable

Acceptance:

- Same replay + same build + same settings => same frame hash sequence.

---

## 3.2 Add Trace Session Lifecycle

Implement MCP tools:

- `trace_start`
- `trace_stop`
- `trace_export_manifest`
- `trace_summary`

Trace session options:

- `subsystems`: `ge`, `audio`, `memory`, `state`
- `start_frame`
- `max_frames`
- `output_dir`

At export, generate a trace manifest:

- `schema`
- `game_id`
- `build_id`
- `ppsspp_version`
- `settings_fingerprint`
- `start_frame` / `end_frame`
- file paths for each artifact stream
- per-file SHA-256

Acceptance:

- A single tool call can return the manifest path and structured summary.

---

## 3.3 Add First-Divergence Diff Tools

Implement MCP tools:

- `diff_trace`
- `find_first_divergence`

Inputs:

- two manifest paths (`reference`, `target`)
- subsystem (`ge`, `audio`, `memory`, `state`, `frame_hash`)

Output:

- earliest frame/call index where mismatch appears
- field-level diff details
- short context window before and after divergence
- links/paths to source events

Normalization rules:

- normalize unstable tokens (timestamps, host paths, transient IDs)
- optionally mask pointer-like addresses when comparing logical behavior

Acceptance:

- For mismatching traces, tool returns deterministic first divergence location.

---

## 3.4 Add Hash-Based State Probes

Implement MCP tools:

- `frame_hash`
- `memory_region_hash`
- `state_fingerprint`

Use cases:

- fast equality checks before deep diffs
- regression gates in automated loops

Acceptance:

- Hash tools return stable output across identical deterministic runs.

---

## 3.5 Add MCP Resources for Read-Only Context

Keep actions as tools, and expose read-only state via resources:

- `ppsspp://capabilities`
- `ppsspp://session`
- `ppsspp://game`
- `ppsspp://memory-map`
- `ppsspp://trace-schema`

Why:

- reduces unnecessary tool calls
- better aligns with MCP design (`resources` for context, `tools` for actions)

Acceptance:

- Agent can inspect environment and schema details without side effects.

---

## 3.6 Tighten Validation and Error Contracts

Server-side validation must not rely only on advertised schemas.

Add:

- explicit type/range checks for every tool argument
- bounded limits:
  - max memory read size
  - max trace duration/frame count
  - max export payload sizes
- normalized structured error payloads:
  - `code`
  - `message`
  - `hint`
  - `retryable`

Examples:

- reject unknown button names
- reject negative frame counts
- reject invalid file paths outside allowed output roots

Acceptance:

- invalid args produce deterministic, parseable error responses.

---

## 3.7 Improve Connection and Session Management

Add:

- explicit `connect` / `disconnect` lifecycle handling internally
- backend health checks (`backend_status` tool)
- reconnect strategy for dropped WebSocket sessions
- clear timeout classes:
  - request timeout
  - frame step timeout
  - trace flush timeout

Acceptance:

- transient PPSSPP debugger disconnects fail gracefully with recoverable errors.

---

## 3.8 Add Logging and Artifact Hygiene

Rules:

- never write logs to stdout (stdout is MCP protocol transport)
- write logs to stderr or log files only
- include run IDs in logs and artifact names
- rotate or cap artifact volume

Acceptance:

- long runs do not flood memory/disk uncontrollably.

---

## 4. Tool Surface vNext (Proposed)

Keep existing tools, then add:

- `backend_status`
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

Optional later:

- subsystem-specific helpers:
  - `ge_trace_summary`
  - `audio_trace_summary`

---

## 5. Emulator-Side Hook Work (PPSSPP C++ Side)

The MCP server depends on debugger primitives in PPSSPP. Add or stabilize:

- GE trace extraction per frame/call index
- audio event/block trace hooks
- memory allocation/copy trace hooks
- settings fingerprint endpoint
- explicit frame counter/event index in relevant debugger responses

Design rule:

- Keep these as generic debugger capabilities; do not hardcode MCP semantics in
  core emulator modules.

---

## 6. Codex Plugin Integration Requirements

The plugin should remain a wrapper around MCP:

- `.codex-plugin/plugin.json`
- `.mcp.json`
- launcher script
- skill instructions for deterministic debugging workflow

Operational requirements:

- configurable `PPSSPP_DEBUGGER_WS`
- default port alignment docs (`3250` vs `8765`)
- clear startup checks and failure hints in skill docs

---

## 7. Suggested Implementation Phases

## Phase 1: Reliability + Validation

- Harden argument validation and error contracts.
- Add `backend_status`.
- Add structured logging and timeout classes.

Deliverable:

- safer current toolset with predictable failures.

## Phase 2: Deterministic Replay + Hashes

- input record/replay tools
- `frame_hash`, `memory_region_hash`, `state_fingerprint`

Deliverable:

- reproducible run primitives and quick equivalence checks.

## Phase 3: Trace Sessions + Manifest

- `trace_start`, `trace_stop`, `trace_export_manifest`, `trace_summary`
- JSONL streams + manifest schema v1

Deliverable:

- machine-readable evidence bundle per run.

## Phase 4: Diff + First Divergence

- `diff_trace`, `find_first_divergence`
- normalization layer

Deliverable:

- automated isolation of the first real behavioral split.

## Phase 5: Resource Surface + Cleanup

- MCP resources for read-only context
- UX cleanup, docs, and test hardening

Deliverable:

- complete spec-aligned server + plugin workflow.

---

## 8. Test Plan

## 8.1 MCP Protocol Tests

- initialize/shutdown behavior
- tools listing
- invalid method handling
- malformed request handling

## 8.2 Backend Integration Tests

- PPSSPP unavailable -> actionable error
- PPSSPP available -> state/frame/step operations
- disconnect/reconnect scenarios

## 8.3 Determinism Tests

- run replay twice from same checkpoint
- compare frame hash sequence equality
- compare key memory region hashes

## 8.4 Trace/Diff Tests

- generate two identical traces -> no divergence
- inject controlled mutation -> first divergence found at expected frame/index

## 8.5 Safety Tests

- boundary checks on memory reads and frame counts
- path validation for exported artifacts

---

## 9. Definition of Done

The PPSSPP-MCP server is considered upgraded when:

1. Deterministic replay and trace sessions are fully tool-driven.
2. Evidence manifests and JSONL traces are produced and consumable.
3. First-divergence diff tooling works end-to-end.
4. Resource/tool split is implemented for read-only vs side-effect operations.
5. Validation, limits, and structured errors are enforced server-side.
6. Codex plugin packaging works as the default distribution path.
7. Automated tests cover protocol, integration, determinism, and diff workflows.

---

## 10. Non-Goals (For This Upgrade)

- Replacing PPSSPP debugger WebSocket protocol with MCP directly.
- Embedding full MCP runtime into PPSSPP executable.
- Building UI-heavy in-emulator debug menus before trace/diff pipeline maturity.

The focus is reliability, reproducibility, and evidence-first debugging for
developer agents.
