# modullum — Sequencing & Workflow Concept

---

## Workflow Overview

The system operates on a **propose → approve → execute → surface** cycle. It is always-on and works autonomously through batched tasks during downtime, surfacing proposals to the user at natural checkpoints rather than blocking on human input during execution.

```
User fires work package
        ↓
Scope Manager classifies blast radius
        ↓
Modules execute (pipeline + internal validation)
        ↓
Results surface as new proposals
        ↓
User reviews, approves or rejects
        ↓
Impact analysis triggers on approval
        ↓
         [repeat]
```

The user is **never in the critical path of execution** — they interact at the proposal boundary only.

---

## Batched Execution Model

Rather than executing tasks as they arrive, the system groups work by type and processes in waves. This keeps the same model loaded and warm across a batch, maximising local inference efficiency.

```
Code generation batch completes
        ↓
Review batch runs against generated code
        ↓
Documentation batch updates affected docs
        ↓
Re-proposal batch surfaces new work packages
        ↓
User check-in: review proposals, approve/reject/modify
        ↓
Impact analysis + test trigger on approval
        ↓
         [next wave]
```

Batching by task type also ensures coherent context within a batch — nodes working on related tasks share a relevant state snapshot rather than cold-starting each time.

---

## Work Package Lifecycle

A work package is the unit of user-authorised work. The system proposes them; the user fires them.

```
PROPOSED → FIRED → IN PROGRESS → COMPLETE → ARCHIVED
                         │
                    [validation fail]
                         ↓
                     REWORK (within module, not visible to user)
                         ↓
                    [validation pass]
                         ↓
                      COMPLETE
```

Rework loops are contained within modules. The user only ever sees work packages in PROPOSED or COMPLETE state — the internal iteration is invisible.

---

## Sequencing Logic

Module sequencing within a fired work package is partly fixed (by dependency) and partly dynamic (by scope classification). The Scope Manager determines which modules fire and in what depth.

```
Work Package fires
        ↓
Scope Manager → classifies level (function / script / module / codebase / ...)
        ↓
Dependency graph consulted → resolves required module sequence
        ↓
Modules fire in order, each self-validating before passing output forward
        ↓
Non-conformances caught mid-sequence → escalate to re-scope or surface to user
        ↓
Package completion signal → new proposals generated
```

---

## Terminal UI Concept

For early development, a TUI (terminal user interface) is the right environment — fast to iterate, no frontend overhead, and well-suited to the structured, panel-based information the system surfaces.

The aesthetic is **htop, not bash** — panelled, live-updating, keyboard-navigable.

```
┌─ modullum ────────────────────────────────────────────────────────────────────┐
│                                                                               │
│  ┌─ Proposals ──────────────────────────┐  ┌─ Package Detail ──────────────┐ │
│  │  ● WP-041  script-level    ready     │  │  ID:      WP-041              │ │
│  │  ○ WP-042  module-level    ready     │  │  Scope:   script-level        │ │
│  │  ○ WP-043  function-level  pending   │  │  Target:  auth/session.py     │ │
│  │  ○ WP-044  codebase-level  blocked   │  │  Trigger: NCR-012             │ │
│  └──────────────────────────────────────┘  │                               │ │
│                                            │  Changes proposed:            │ │
│  ┌─ Queue ──────────────────────────────┐  │   + add token refresh logic   │ │
│  │  WP-039   review     ████████░░  80% │  │   ~ refactor expiry handling  │ │
│  │  WP-040   codegen    ██████████ done │  │                               │ │
│  │  WP-038   docs       ██████████ done │  │  Impact:  WP-044 depends on   │ │
│  └──────────────────────────────────────┘  │           this package        │ │
│                                            │  Est:     4 nodes  ~12 min    │ │
│  ┌─ System ─────────────────────────────┐  │                               │ │
│  │  Model:   granite4:3b (warm)         │  │  [F] Fire  [R] Reject         │ │
│  │  Queue:   3 tasks pending            │  │  [M] Modify scope             │ │
│  │  Uptime:  4h 22m                     │  └───────────────────────────────┘ │
│  └──────────────────────────────────────┘                                    │
│                                                                               │
│  [↑↓] navigate   [enter] select   [f] fire   [r] reject   [q] quit           │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Proposal Detail Drill-down

Selecting a proposal expands into a full-screen detail view:

```
┌─ WP-041 Detail ───────────────────────────────────────────────────────────────┐
│                                                                               │
│  Scope: script-level                     Triggered by: NCR-012               │
│  Target: auth/session_manager.py         Requirement:  COMP-AUTH-SM-001      │
│                                                                               │
│  ┌─ Proposed Changes ───────────────────────────────────────────────────────┐ │
│  │  + implement token refresh on expiry (ref: UNIT-AUTH-SM-TR-001)          │ │
│  │  ~ refactor _check_expiry() → returns bool, currently raises             │ │
│  │  ~ update call sites within script (3 locations)                         │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                               │
│  ┌─ Impact Chain ───────────────────────────────────────────────────────────┐ │
│  │  auth/session_manager.py                                                 │ │
│  │    └── WP-044 (module-level) — blocked pending this package              │ │
│  │    └── tests/test_session.py — test run will trigger on approval         │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                               │
│  ┌─ Scope Rationale ────────────────────────────────────────────────────────┐ │
│  │  Classified script-level: change is contained within one file.           │ │
│  │  Interface boundary unchanged. No upstream requirement revision needed.  │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                               │
│  [F] Fire    [R] Reject    [M] Modify scope    [B] Back                       │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Live Execution View

When a work package is in flight:

```
┌─ WP-041 Executing ────────────────────────────────────────────────────────────┐
│                                                                               │
│  auth/session_manager.py — script-level                                       │
│                                                                               │
│  [■■■■■■■■░░░░░░░░░░░░]  Node 2 of 4                                          │
│                                                                               │
│  ✓  scope-manager        classified: script-level          0:04               │
│  ✓  context-assembler    loaded 3 req artefacts, 1 NCR     0:08               │
│  ►  code-generation      generating patch...               0:23 (running)     │
│  ○  review               waiting                                              │
│                                                                               │
│  Log:                                                                         │
│  > Loaded COMP-AUTH-SM-001, UNIT-AUTH-SM-TR-001                               │
│  > NCR-012 context attached                                                   │
│  > Generating against session_manager.py (218 lines)                          │
│                                                                               │
│  [P] Pause    [Q] Quit to background                                          │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## Web UI Concept (Future State)

Once the proposal artefact format stabilises, a web UI becomes the natural home for the information-rich review experience the proposal gate requires. The terminal UI informs the layout — same panels, same information hierarchy, richer rendering.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  modullum                                    ● 3 proposals ready   ⚙  ···   │
├─────────────────┬────────────────────────────┬───────────────────────────────┤
│  PROPOSALS      │  WP-041                    │  IMPACT CHAIN                 │
│                 │  script-level · ready      │                               │
│  ● WP-041  →   │                            │  session_manager.py           │
│  ○ WP-042      │  Target                    │    └─ WP-044  blocked         │
│  ○ WP-043      │  auth/session_manager.py   │    └─ test_session.py         │
│                 │                            │         will trigger          │
│  QUEUE          │  Changes                   │                               │
│                 │  + token refresh logic     │  SCOPE RATIONALE              │
│  WP-039  80%   │  ~ refactor expiry()       │                               │
│  WP-040  done  │  ~ update 3 call sites     │  Contained within one file.   │
│  WP-038  done  │                            │  Interface boundary stable.   │
│                 │  Requirement               │  No upstream revision needed. │
│  SYSTEM         │  COMP-AUTH-SM-001          │                               │
│  granite4:3b   │  UNIT-AUTH-SM-TR-001       │                               │
│  4h uptime     │                            │                               │
│                 │  [ Fire ]  [ Reject ]      │                               │
│                 │  [ Modify scope ]          │                               │
├─────────────────┴────────────────────────────┴───────────────────────────────┤
│  Est. 4 nodes · ~12 min · 2 test files will trigger on approval              │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Key principles for the web UI:**
- Proposals are documents, not commands — the UI should support reading and reasoning, not just approving
- Scope rationale is always visible — the user should be able to assess whether the scope manager got it right at a glance
- Impact chain is first-class — downstream consequences are shown before the user fires, not after
- Batch firing is a future consideration — initially single-package approval keeps review quality high

---

## Engagement Layer

The user-facing surface is separated from the execution system by an **engagement layer**. This decouples the always-on background execution from the user interaction cadence.

```
Execution system (always-on, batching)
        ↓  ↑
Engagement layer  ←→  User
        │
        ├── Surfaces proposals for review
        ├── Accepts fire / reject / modify scope signals
        ├── Displays queue state and execution progress
        └── Receives requirements change input
```

The engagement layer means the system never needs to be fast — it needs to be thorough. Latency is absorbed by the async execution model. The user checks in on their own schedule.

---

*modullum — internal concept document*
