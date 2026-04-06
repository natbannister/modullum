# Modullum Architecture Refactor

## Context

The immediate catalyst for this refactor was considering whether to delegate UI development to devstral-small-2. However, the real issue uncovered was deeper: **Modullum's observability layer is tightly coupled to its execution logic**, making it impossible to cleanly add new interfaces (web, API, etc.) without forking or conditionally branching the entire codebase.

Development should not continue on the current architecture. The terminal-based implementation works, but extending it means propagating the existing coupling further into new modules.

## The Problem: Tangled Observability

Current state of the codebase:

- **`head.py`**: Orchestrator directly calls `logger.info()` and knows about tmux
- **Module files** (`requirements_gen.py`, `code_gen.py`): Business logic mixed with:
  - Direct `logger.info()` calls
  - Rich spinner initialization (`status_spinner()`)
  - Tmux pane management (`StreamDisplay()`)
  - Manual user wait time tracking from `get_input()`
- **`nodes.py`**: Core LLM call logic writes directly to `sys.stdout` or accepts a custom writer
- **`pane_display.py`**: Tmux-specific split pane logic with fallback to logger/stdout

**Consequence**: The "what happened" (execution events) is inseparable from "how to show it" (rendering). Adding a web UI would require either:
1. Duplicating module logic with web-specific I/O, or
2. Adding `if web_mode:` conditionals throughout the engine

Both are unacceptable.

## The Solution: Event-Driven Architecture

Separate execution from observation by introducing an **event bus**. The engine emits structured events; UI layers subscribe and render independently.

### Proposed Architecture

```
┌─────────────────────────────────────────────┐
│              head.py                        │
│        (Pure orchestration)                 │
└──────────────┬──────────────────────────────┘
               │ invokes
               ▼
┌──────────────────────────────────────────────────────────┐
│  Modules (requirements_gen, code_gen, scope_manager)     │
│  • Pure business logic                                   │
│  • No logger, no Rich, no tmux, no sys.stdout           │
│  • Emit events via ctx.events.emit()                     │
└──────────────┬───────────────────────────────────────────┘
               │ emits events
               ▼
┌──────────────────────────────────────────────┐
│            EventBus                          │
│   Broadcasts to all subscribers              │
└──────┬───────────────────────────────────┬───┘
       │                                   │
       ▼                                   ▼
┌──────────────────┐              ┌──────────────────┐
│ Terminal         │              │ WebSocket        │
│ Subscriber       │              │ Subscriber       │
│                  │              │                  │
│ Rich + tmux      │              │ Browser display  │
│ rendering        │              │ via websocket    │
└──────────────────┘              └──────────────────┘
```

### Key Properties

1. **Engine is hermetically sealed**: No I/O dependencies. Modules emit events; they don't know or care who's listening.

2. **UI consumers are equal peers**: Terminal and web UI both subscribe to the same event stream. Neither is privileged.

3. **Extensibility**: Add new subscribers (Slack bot, metrics dashboard, test harness) without touching engine code.

4. **Existing file-based recording unchanged**: `ModuleContext` and `NodeRecord` continue writing JSON logs to disk. Events are additive, not a replacement.

## Event Schema

Events are the contract between engine and UI. They encode "what happened" in structured form:

### Run-level events
- `run_started` — `{run_id, timestamp}`
- `run_completed` — `{run_id, exit_reason, duration_s}`

### Module-level events
- `module_started` — `{module, timestamp}`
- `module_completed` — `{module, exit_reason, duration_s}`

### Node-level events
- `node_started` — `{module, role, prompt, model}`
- `node_completed` — `{module, role, tokens_in, tokens_out, llm_duration_s, exit_reason}`
- `llm_streaming_chunk` — `{module, role, content}` (optional, for live streaming)

### User interaction events
- `user_input_requested` — `{prompt}`
- `user_input_received` — `{text, wait_s}`

### Status events
- `requirements_accepted` — `{requirements_count}`
- `assumptions_raised` — `{assumptions}`
- `code_generated` — `{file_path}`
- `test_result` — `{file_path, passed, failed}`

Schema should be finalized during implementation based on what information UI consumers actually need.

## Implementation Plan

### Phase 1: Add EventBus Infrastructure

**Files to create:**
- `modullum/core/events.py` — EventBus class with subscribe/emit methods

**Files to modify:**
- `modullum/core/workspace.py` — Thread EventBus through `RunContext` and `ModuleContext`

```python
# events.py
class EventBus:
    def __init__(self):
        self._subscribers = []
    
    def subscribe(self, subscriber):
        self._subscribers.append(subscriber)
    
    def emit(self, event_type: str, data: dict):
        for subscriber in self._subscribers:
            subscriber.handle(event_type, data)

# workspace.py modifications
class ModuleContext:
    def __init__(self, ..., event_bus: EventBus):
        self.events = event_bus
        # ... rest of init
```

### Phase 2: Make Recording Methods Emit Events

**Files to modify:**
- `modullum/core/workspace.py`

Update `ModuleContext.start_node()` and `ModuleContext.record_node()` to emit events:

```python
def start_node(self, role, prompt, model, ...):
    rec = NodeRecord(...)
    self.events.emit("node_started", {
        "module": self.name,
        "role": role,
        "prompt": prompt,
        "model": model,
    })
    return rec

def record_node(self, rec):
    self._nodes.append(rec)
    self.events.emit("node_completed", {
        "module": self.name,
        "role": rec.role,
        "tokens_in": rec.tokens_in,
        "tokens_out": rec.tokens_out,
        "llm_duration_s": rec.llm_duration_s,
        "exit_reason": rec.exit_reason,
    })
```

### Phase 3: Remove Direct I/O from Modules

**Files to modify:**
- `modullum/modules/requirements_gen.py`
- `modullum/modules/code_gen.py`
- Other module files

**Changes:**
- Remove all `logger.info()` calls
- Remove `status_spinner()` calls
- Remove direct `StreamDisplay()` usage
- Replace with `ctx.events.emit()` calls

**Example transformation:**
```python
# Before:
logger.info("Requirements accepted.\n")

# After:
ctx.events.emit("requirements_accepted", {"count": len(requirements_json.requirements)})
```

### Phase 4: Build Terminal Subscriber

**Files to create:**
- `modullum/ui/terminal_subscriber.py`

**Files to modify:**
- `modullum/main.py` — Wire up terminal subscriber to event bus

```python
# terminal_subscriber.py
from rich.console import Console

class TerminalSubscriber:
    def __init__(self):
        self.console = Console()
        self._spinner = None
    
    def handle(self, event_type: str, data: dict):
        if event_type == "module_started":
            self.console.print(f"[bold]{data['module']}[/bold]")
        
        elif event_type == "node_started":
            self._spinner = self.console.status("Thinking...")
        
        elif event_type == "node_completed":
            if self._spinner:
                self._spinner.stop()
                self._spinner = None
        
        elif event_type == "requirements_accepted":
            self.console.print(f"\n✓ {data['count']} requirements accepted\n")
        
        elif event_type == "llm_streaming_chunk":
            self.console.print(data['content'], end='')
        
        # ... handle other events
```

This restores the current terminal UX as a pure consumer of events.

### Phase 5: Build WebSocket Subscriber (Future)

**Files to create:**
- `modullum/ui/websocket_server.py`
- `modullum/ui/websocket_subscriber.py`

```python
# websocket_subscriber.py
class WebSocketSubscriber:
    def __init__(self, websocket):
        self.ws = websocket
    
    def handle(self, event_type: str, data: dict):
        self.ws.send_json({
            "type": event_type,
            "data": data,
            "timestamp": time.time(),
        })
```

Browser connects via WebSocket, receives live event stream, renders in React/vanilla JS.

### Phase 6: Cleanup

**Files to remove or archive:**
- `modullum/core/pane_display.py` — Tmux logic moves into terminal subscriber
- `modullum/core/terminal.py` — `StreamingConsoleHandler` becomes subscriber concern

**Files to modify:**
- `modullum/core/nodes.py` — Remove `stream_display` parameter from `call_node()`; streaming becomes an event emission pattern

## Benefits

1. **Cleaner codebase**: Engine modules contain only business logic. No Rich imports, no logger imports, no tmux knowledge.

2. **True separation of concerns**: "What happened" (events) vs "how to display it" (subscribers) are decoupled.

3. **Testability**: Can subscribe a test harness that asserts on event sequences without any UI rendering.

4. **Multiple UIs simultaneously**: Run terminal + web UI at the same time for debugging.

5. **Future self-improvement layer**: Event stream becomes the raw material for analyzing run history, tuning prompts, detecting patterns.

## What Doesn't Change

- **File-based recording**: Run directories, JSON logs, prompt files all stay exactly as-is
- **Module return values**: `RequirementsList`, `CodegenResult`, etc. remain the module interface
- **`RunContext` / `ModuleContext` structure**: These continue to own timing, recording, and output paths
- **Existing functionality**: Users see the same behavior; the refactor is internal

## Validation

After Phase 4 (terminal subscriber), Modullum should behave identically to the current version from the user's perspective. If it doesn't, the refactor has a bug.

After Phase 5 (websocket subscriber), both terminal and web UI should show the same information in real-time, proving the event bus works.

## Future: UI Development with devstral-small-2

Once the event bus refactor is complete, the web UI becomes a clean, bounded task to delegate:

**Scope**: Build a local web app that:
- Runs a WebSocket server (Python FastAPI/Flask)
- Subscribes to EventBus
- Serves a single-page frontend (React/vanilla JS)
- Displays live event stream in structured panes

**Why this is now viable**:
- UI work is completely separate from engine logic
- Spec is tight: "consume these events, render this way"
- If devstral produces poor code, it's isolated — engine is unaffected
- Proves whether small models can handle bounded infrastructure tasks

**Why it wasn't viable before**:
- UI requirements were unclear (what signals matter?)
- Engine and display were coupled (changes would ripple)
- Handing off would mean handing off core architecture decisions

The event schema *is* the UI specification. Once that's formalized, UI development becomes a rendering problem, not an architectural one.

## Immediate Next Steps

1. **Pause feature development** — Do not add new modules or functionality until this refactor is complete
2. **Implement Phases 1-4** — Event bus, emission points, terminal subscriber
3. **Validate** — Confirm terminal behavior matches current UX exactly
4. **Document event schema** — Formalize the contract for future UI work
5. **Then resume** — Continue building Modullum's core with a clean foundation

The refactor is not scope creep. It's architectural hygiene that unblocks both internal development and potential delegation to smaller models.