# PPSSPP MCP Adapter — Architecture Reference

This document covers the hook locations in the PPSSPP C++ codebase, the
WebSocket debugger IPC protocol, and the MCP tool surface exposed to AI agents.

---

## 1. Hook Locations in PPSSPP

### 1.1 Frame Stepping — `cpu.stepFrame`

**File:** `Core/Debugger/WebSocket/SteppingSubscriber.cpp`
**Method:** `WebSocketSteppingState::Frame()`
**Registered in:** `WebSocketSteppingInit()` dispatch table setup.

When the CPU is in stepping mode, this command calls:
```cpp
Core_RequestCPUStep(CPUStepType::Frame, 0)
```
which queues a single-frame advance in the PSP CPU execution loop.
Completion is signalled asynchronously via the `cpu.stepping` WebSocket event
with `reason: "frame.after"`.

**Relevant core files:**
- `Core/Core.h` / `Core/Core.cpp` — `Core_RequestCPUStep`, `Core_IsStepping`,
  `Core_Resume`, `Core_UpdateSingleStep`
- `Core/CoreParameter.h` — `CPUStepType` enum (`Frame`, `Into`, `Over`, ...)

---

### 1.2 GPU Buffer Capture — `gpu.buffer.screenshot` / `gpu.buffer.renderColor`

**File:** `GPU/Debugger/Stepping.cpp`
**Key functions:**
- `GPU_GetOutputFramebuffer()` — captures the final display output buffer
- `GPU_GetCurrentFramebuffer()` — captures the currently-bound render target
- `ProcessStepping()` — executes queued GPU pause actions on the GPU thread

**Modified behaviour (this session):** `ProcessStepping()` now executes GPU
buffer capture actions when the core is in either `CORE_STEPPING_GE` **or**
`CORE_STEPPING_CPU`. Previously, CPU-owned stepping mode (`CORE_STEPPING_CPU`)
blocked all GPU capture requests.

The key change in `ProcessStepping()`:
```cpp
// Accepts both GE-owned and CPU-owned stepping modes
if (coreState != CORE_STEPPING_GE && coreState != CORE_STEPPING_CPU) {
    // Not stepping; skip
    ...
}
// Special-case: if action is PAUSE_CONTINUE and CPU owns stepping,
// just acknowledge — don't transition coreState to CORE_RUNNING_GE
if (pauseAction == PAUSE_CONTINUE && coreState == CORE_STEPPING_CPU) {
    actionComplete = true;
    actionWait.notify_all();
    return false;
}
```

**WebSocket handler:** `Core/Debugger/WebSocket/GPUBufferSubscriber.cpp`

---

### 1.3 CPU Pause — `cpu.stepping`

**File:** `Core/Debugger/WebSocket/CPUCoreSubscriber.cpp`
**Method:** `WebSocketCPUStepping()`

When called, requests a CPU breakpoint immediately. Modified guard: the break
is requested whenever `!Core_IsStepping()` (does not require `Core_IsActive()`).

---

### 1.4 Input Injection

**File:** `Core/Debugger/WebSocket/InputSubscriber.cpp`

Two input modes are supported:
- `input.buttons.send` — set button state explicitly (`pressed: true/false`)
- `input.buttons.press` — pulse a button for N frames and auto-release

These commands inject directly into PPSSPP's `__CtrlSetButtons` / `__CtrlButtonDown`
HLE layer, bypassing SDL/OS input.

---

## 2. WebSocket IPC Protocol

PPSSPP exposes a WebSocket server at `ws://127.0.0.1:<port>/debugger`
with subprotocol `debugger.ppsspp.org`.

### 2.1 Enabling the Debugger

Use `--appendconfig=<file>` at launch with an ini fragment:
```ini
[General]
RemoteISOPort = 8765
RemoteDebuggerOnStartup = True
RemoteDebuggerLocal = True
```
`remote_debugger.ini` in this directory contains this configuration.

### 2.2 Message Format

All messages are JSON objects sent as WebSocket text frames.

**Request (client → PPSSPP):**
```json
{
  "event": "<command_name>",
  "ticket": "<uuid>",
  "<param_key>": "<param_value>"
}
```

**Response (PPSSPP → client):**
```json
{
  "event": "<command_name>",
  "ticket": "<uuid>",
  "<result_key>": "<result_value>"
}
```

**Error response:**
```json
{
  "event": "error",
  "ticket": "<uuid>",
  "message": "<human-readable error>"
}
```

**Unsolicited event (no ticket):**
```json
{
  "event": "cpu.stepping",
  "reason": "frame.after",
  "stepping": true,
  "pc": 12345678
}
```

### 2.3 Key Commands

| Command | Parameters | Response fields |
|---------|-----------|-----------------|
| `version` | `name`, `version` | `name`, `version` |
| `cpu.status` | — | `pc`, `ticks`, `stepping`, `started` |
| `cpu.stepping` | — | (async `cpu.stepping` event) |
| `cpu.resume` | — | (async `cpu.stepping` event with `stepping:false`) |
| `cpu.stepFrame` | — | `accepted: true` (async `cpu.stepping` completion) |
| `game.status` | — | `game`, `id`, `title`, `started` |
| `gpu.buffer.screenshot` | `type:"uri"`, `alpha:bool` | `uri`, `width`, `height` |
| `gpu.buffer.renderColor` | `type:"uri"`, `alpha:bool` | `uri`, `width`, `height` |
| `input.buttons.send` | `buttons:{<name>:bool}` | `buttons` |
| `input.buttons.press` | `button:<name>`, `duration:<frames>` | `button`, `duration` |
| `memory.read` | `address`, `size` | `base64` |
| `game.reset` | `break` (optional) | none (ack only) |
| `savestate.save` | `slot` (0-4) | `slot`, `size` |
| `savestate.load` | `slot` (0-4) | `slot`, `size` |
| `savestate.list` | — | `slots[]` |

### 2.4 Stepping Flow

```
Client                          PPSSPP
  │                               │
  ├─ cpu.stepping ──────────────► │  (pause CPU)
  │ ◄───────────── cpu.stepping ──┤  {reason:"halt", stepping:true}
  │                               │
  ├─ gpu.buffer.screenshot ─────► │  (capture framebuffer)
  │ ◄─────────────── {uri:...} ───┤
  │                               │
  ├─ input.buttons.press ───────► │  (queue button pulse)
  │ ◄────────────── {button:...}──┤
  │                               │
  ├─ cpu.stepFrame ─────────────► │  {accepted:true}
  │ ◄───────────── cpu.stepping ──┤  {reason:"frame.after", stepping:true}
  │                               │
```

---

## 3. MCP Tool Surface

The MCP server (`ppsspp_mcp_server.py`) exposes multiple tools over MCP stdio
(JSON-RPC with `Content-Length` framing, protocol version 2025-03-26).

### 3.1 `get_frame`

Capture the current display output as a PNG image.

**Input schema:**
```json
{ "alpha": { "type": "boolean", "description": "Include alpha channel" } }
```

**Output:** MCP tool result with one image content item:
```json
{
  "type": "image",
  "data": "<base64 PNG>",
  "mimeType": "image/png"
}
```

**Implementation:** Calls `ensure_stepping()` then tries `gpu.buffer.screenshot`,
falling back to `gpu.buffer.renderColor`. On recoverable errors retries up to 3
times with an intermediate `step_frame(1)` to flush the GPU pipeline.

---

### 3.2 `step_frame`

Advance emulation by one or more frames while remaining paused.

**Input schema:**
```json
{ "count": { "type": "integer", "description": "Frames to advance (default 1)" } }
```

**Output:**
```json
{
  "type": "text",
  "text": "{\"count\":1, \"pc\":12345678, \"ticks\":9876543210, \"stepping\":true}"
}
```

**Implementation:** Calls `ensure_stepping()`, then `cpu.stepFrame`, then polls
`cpu.status` until `ticks` advances, confirming frame completion.

---

### 3.3 `press_button`

Inject a PSP button press.

**Input schema:**
```json
{
  "button": { "type": "string" },
  "pressed": { "type": "boolean", "description": "Set state (omit for pulse)" },
  "duration": { "type": "integer", "description": "Pulse duration in frames" }
}
```

Valid button names: `cross`, `circle`, `triangle`, `square`, `up`, `down`,
`left`, `right`, `start`, `select`, `ltrigger`, `rtrigger`, and others.

**Output:** JSON describing the mode and button used.

**Implementation:**
- If `duration` is provided: uses `input.buttons.press` (auto-release after N frames)
- Otherwise: uses `input.buttons.send` with explicit `pressed` state

---

### 3.4 `get_state`

Return current emulator state (CPU and game status).

**Input schema:** (none)

**Output:**
```json
{
  "type": "text",
  "text": "{\"cpu\":{...}, \"game\":{...}, \"connected\":true, \"ws_url\":\"...\"}"
}
```

---

### 3.5 `read_memory`

Read raw PSP memory as base64.

**Input schema:** `address` (int), `size` (int, 1..1048576)

**Implementation:** Ensures stepping, then calls `memory.read`.

---

### 3.6 `reset_game`

Reset/reboot the current game.

**Input schema:** (none)

**Implementation:** Sends `game.reset` and then refreshes `game.status`.

---

### 3.7 `save_state` / 3.8 `load_state` / 3.9 `list_states`

Manage in-memory savestate slots (0-4).

**Input schema:**
- `save_state`: optional `slot` (default 0)
- `load_state`: optional `slot` (default 0)
- `list_states`: none

**Implementation:**
- `save_state` → `savestate.save`
- `load_state` → `savestate.load`
- `list_states` → `savestate.list`

Slots are currently stored in emulator memory (not persisted to disk).

---

## 4. Agent Loop Utility

`agent_loop.py` is a reference implementation of a minimal observe→act loop:

```
for each iteration:
  1. get_frame()        → PNG + SHA-256 hash
  2. choose_action()    → press button every N iterations
  3. press_button()     → inject input
  4. step_frame()       → advance one frame
  5. log to JSONL       → {iteration, timestamp, frame, action, step}
```

**Usage:**
```bash
python3 Tools/mcp_adapter/agent_loop.py \
  --ws-url ws://127.0.0.1:8765/debugger \
  --iterations 100 \
  --step-count 1 \
  --button cross \
  --action-every 5 \
  --save-frame-every 10 \
  --output-dir Tools/mcp_adapter/loop_logs
```

**Validation results:** 100 iterations, 0 errors, 97 unique frame hashes.
Determinism test (30 frames × 2 independent runs): 30/30 matching hashes.

---

## 5. Determinism Test

`test_determinism.py` validates that identical action sequences produce
byte-identical frame outputs across two independent PPSSPP runs from cold boot.

```bash
python3 Tools/mcp_adapter/test_determinism.py \
  --iso /path/to/game.PBP \
  --iterations 30 \
  --action-every 5 \
  --boot-wait 12
```

Expected output: `PASS: Both runs produced identical frame sequences.`
