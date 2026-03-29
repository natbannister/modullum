# modullum — Context Architecture, Storage & NCR Concept

---

## The Core Problem

As a codebase grows, project state will vastly exceed any node's context window. The
system cannot rely on an LLM to reason across the full requirements set, full codebase,
and full history simultaneously — nor should it. The solution is to make the
**retrieval and assembly layer smarter than the models**, so each node receives exactly
the slice it needs and nothing more.

This is the distinction that makes deterministic operation at scale possible:

```
Naive approach:    LLM reasons across everything  →  expensive, slow, degrades at scale
modullum approach: System assembles exact slice   →  LLM performs narrow task reliably
```

---

## Multi-Database Storage Architecture

No single storage mechanism is appropriate for all data types in the system. Three
complementary stores are used, each suited to a different retrieval pattern.

```
┌──────────────────────────────────────────────────────────┐
│                    Retrieval Router                       │
│         (determines what slice each node gets)           │
└───────────────┬─────────────────┬────────────────────────┘
                │                 │                  │
                ▼                 ▼                  ▼
        ┌───────────┐    ┌──────────────┐    ┌────────────┐
        │ Structured│    │   Vector DB  │    │  Graph DB  │
        │   Store   │    │  (semantic)  │    │(relations) │
        │  SQLite   │    │   ChromaDB   │    │  SQLite /  │
        │           │    │  / LanceDB   │    │  NetworkX  │
        └───────────┘    └──────────────┘    └────────────┘
                │                 │                  │
                └─────────────────┴──────────────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │  Context Assembler  │
                       │ (condenses, orders, │
                       │     formats)        │
                       └─────────────────────┘
                                  │
                                  ▼
                          Node context window
```

### Structured Store (SQLite)

The source of truth for all exact, deterministic lookups. If something has a known
schema and a known retrieval key, it lives here.

**Stores:**
- Current and versioned requirements (with status, version, parent ID)
- Work package definitions, status, scope classification, audit trail
- NCR records (ID, triggering requirement, target file, scope level, status)
- Scope manager decisions and rationale logs
- Node execution logs and performance data
- Test results and verification records
- Interface definitions between components

**Retrieval pattern:** exact lookup by ID, path, version, or status. No model involved.

### Vector Database (ChromaDB / LanceDB)

Used for semantic similarity retrieval across large document sets where exhaustive
indexing is not practical. The mistake is using this as the only retrieval layer —
it is one tool among three.

**Good for:**
- "Find historical NCRs similar to this one"
- "Which past review summaries relate to this component?"
- "Find requirements semantically related to this change description"

**Not appropriate for:**
- Exact lookups ("what is the current version of COMP-AUTH-SM-001?")
- Structured relationship traversal
- Anything requiring guaranteed precision

### Graph Database (SQLite adjacency / NetworkX)

The most underrated store in the system. modullum's data is fundamentally relational:
requirements have parents and children, work packages have dependencies, NCRs trace to
requirements, files depend on interfaces. A graph structure makes impact traversal
deterministic and fast without any model reasoning.

**Stores:**
- Requirement traceability links (SYS → SUB → COMP → UNIT)
- Work package dependency chains
- File-to-requirement mappings
- Interface ownership (which component owns which interface)
- NCR-to-requirement and NCR-to-file relationships

**Retrieval pattern:** traverse from a node outward — "what does this file depend on?",
"what requirements does this function satisfy?", "what work packages are blocked by this
NCR?" — all answered deterministically by graph traversal.

---

## Context Assembly

The Context Assembler is the deterministic intelligence layer of the system. It knows,
given a node's role and its current task, exactly what to fetch from each store and how
to structure it. No model is involved in assembly — it is pure retrieval logic.

### Assembly Template (per node type)

Each node type has a defined context template. Example for a review node:

```python
context = {
    "requirements": structured_store.get_requirements(
        ids=graph.get_requirements_for_file(scope.target),
        version="current"
    ),
    "target_code":  file_store.get(scope.target),
    "prior_ncrs":   vector_db.query(
                        query=scope.target,
                        filter={"status": "open"},
                        limit=3
                    ),
    "dependencies": graph.get_dependencies(scope.target),
    "interfaces":   structured_store.get_interfaces(scope.target),
    "work_package": structured_store.get_wp(wp_id)
}
```

The node receives exactly this — no more, no less. The context window is not filled
with irrelevant history or the full codebase. The model performs its narrow task
against a well-bounded, relevant slice.

### Scope-Driven Context Depth

The scope classification from the Scope Manager directly controls how much context
is assembled. Higher scope levels pull broader context; lower scope levels are surgical.

| Scope Level | Context Assembled |
|---|---|
| Function-level | Target function + its direct requirement(s) + immediate call sites |
| Script-level | Full file + script-level requirements + interface definitions |
| Module-level | Component files + component requirements + dependency graph slice |
| Codebase-level | Cross-cutting requirements + interface map + affected component list |
| Project / Longitudinal | Requirements hierarchy summary + change history + full traceability |

This means a function-level NCR fix costs a tiny fraction of a codebase-level review —
the system is inherently efficient because scope drives cost.

---

## Context Condensation

Even a correctly scoped context slice may exceed a node's context window for larger
scope levels. Condensation strategies reduce size without losing the information the
node actually needs.

### Hierarchical Summarisation

Requirements exist at multiple levels of detail. A codebase-level node does not need
the full text of every unit requirement — it needs the subsystem summary. Summaries
are generated once, cached, and invalidated only when the source document changes.

```
SYS-001 (full text, ~800 words)
    └── Cached summary (~60 words)   ← used by codebase/project-level nodes

COMP-AUTH-SM-001 (full text, ~200 words)
    └── Cached summary (~30 words)   ← used by module-level nodes

UNIT-AUTH-SM-TR-001 (full text, ~80 words)
    └── Used directly               ← used by function/script-level nodes
```

Summaries are generated by a lightweight summarisation node — a cheap, fast model
pass — not the primary task model.

### Diff-Based Context

Many nodes only need to understand *what changed* since the last checkpoint, not the
full current state. Rather than passing entire files or requirement documents, the
assembler computes and passes structured diffs.

```python
# Instead of: full file (800 lines)
# Assembler provides:
context["target_code"] = {
    "diff_since": last_review_checkpoint,
    "changed_functions": ["_check_expiry", "refresh_token"],
    "unchanged_summary": "218 lines, auth session management, last reviewed WP-038"
}
```

### Truncation with Anchoring

When a document must be truncated, the assembler anchors on the sections directly
relevant to the task — the specific requirement clause, the specific function — and
truncates outward from there rather than from the end of the document.

---

## Deterministic Scope & Impact Assessment

The Scope Manager never asks a model to reason about blast radius from scratch. Instead,
it traverses the graph deterministically to find what is actually affected.

### Scope Classification Flow

```
Non-conformance or requirements change arrives
        │
        ▼
Graph lookup: which requirement(s) does the affected file/function map to?
        │
        ▼
Requirement level in hierarchy → maps directly to scope level
(UNIT → function-level, COMP → script/module-level, SUB → codebase-level, etc.)
        │
        ▼
Interface check: does the change cross a component interface boundary?
   Yes → scope escalates one level
   No  → scope confirmed
        │
        ▼
Scope Manager records decision + rationale to structured store
```

### Impact Chain Traversal

Once scope is classified, the graph is traversed outward to find everything affected:

```python
impact = {
    "direct":    graph.get_dependents(target_file),
    "indirect":  graph.get_transitive_dependents(target_file, depth=2),
    "blocked_wps": structured_store.get_wps_depending_on(target_file),
    "test_files": graph.get_test_coverage(target_file),
    "interfaces": structured_store.get_interfaces_owned_by(target_file)
}
```

This produces a fully deterministic impact chain without a model inferring
dependencies from code reading. The graph already knows — because it was populated
when the code was generated.

---

## NCR Detection Without Full Codebase Scrubbing

This is one of the most important properties of the architecture. Raising an NCR does
not require an LLM to read the entire codebase against the entire requirements set.
Instead, NCRs are surfaced through targeted, retrieval-driven review.

### The Problem with Naive NCR Detection

```
Naive: LLM reads all code + all requirements → finds mismatches
Cost:  O(codebase size × requirements size)
Risk:  Hallucination, missed edge cases, cannot scale
```

### modullum's Targeted NCR Approach

NCRs are raised through a series of bounded, deterministic retrieval steps, each
narrowing the search space before any model is involved.

#### Step 1 — Change-Triggered Scope

NCR detection is only triggered in the neighbourhood of a change, not globally.
When code is generated or modified:

```
File changed → graph lookup → which requirements govern this file?
                            → which files depend on this file?
```

This immediately scopes review to a small, relevant subset of the codebase and
requirements set.

#### Step 2 — Requirement-to-Code Mapping

The structured store maintains explicit mappings between requirements and the
code artefacts that implement them. Review is targeted at mapped pairs:

```python
# For a given changed file:
governing_requirements = graph.get_requirements_for_file(changed_file)
# Returns e.g. [COMP-AUTH-SM-001, UNIT-AUTH-SM-TR-001, UNIT-AUTH-SM-EX-002]
# Not the entire requirements set — just the 2-4 directly applicable
```

#### Step 3 — Structured Pre-checks (No Model)

Before any LLM review node fires, the assembler runs deterministic pre-checks:

```python
checks = [
    interface_checker.verify(changed_file, structured_store.get_interfaces()),
    signature_checker.verify(changed_file, previous_version),
    dependency_checker.verify(changed_file, graph.get_dependencies()),
]
# Any structural violation → NCR raised immediately, no model needed
```

Many NCRs can be caught here entirely deterministically — wrong return type, missing
interface implementation, broken import — before an LLM is involved at all.

#### Step 4 — Targeted LLM Review

Only for conformance checks that require semantic understanding (e.g. "does this
implementation satisfy the intent of the requirement?") is a model invoked — and it
receives only the targeted slice:

```
Node context:
  - The changed function or script (not the whole codebase)
  - The 2-4 governing requirements (not the whole requirements set)
  - The interface definition (if relevant)
  - Prior NCRs on this file (from vector DB, limit 3)
```

#### Step 5 — NCR Record Creation

If a non-conformance is detected (by structural check or model review):

```python
ncr = NCR(
    id="NCR-013",
    target_file="auth/session_manager.py",
    target_function="_check_expiry",
    governing_requirement="UNIT-AUTH-SM-TR-001",
    scope_level="function-level",          # from Scope Manager
    detection_method="llm-review",         # or "structural-check"
    work_package=None,                     # to be assigned
    status="open"
)
structured_store.save(ncr)
graph.link(ncr, governing_requirement)
graph.link(ncr, target_file)
```

The NCR is immediately linked into the graph, making it available to future impact
traversals and context assembly without any search.

### NCR Detection Cost Profile

```
Structural checks (no model):   ~milliseconds, catches interface/signature/import NCRs
Targeted LLM review:            ~seconds, 2-4 requirements × 1 file or function
Full codebase scrub (avoided):  ~hours, unreliable, does not scale
```

The architecture means NCR detection cost scales with **change size**, not
codebase size. A small change triggers a small, cheap review. A large refactor
triggers a broader but still scoped review. The full codebase is never the
unit of analysis.

---

## Summary of Key Properties

| Property | How achieved |
|---|---|
| Deterministic scope classification | Graph traversal + requirement hierarchy mapping |
| Exact context per node | Context Assembler templates, no model involved |
| NCR detection without full scrub | Change-triggered, requirement-mapped, graph-bounded |
| Context fits node window | Hierarchical summarisation + diff-based context + anchored truncation |
| Impact chains without inference | Graph traversal from change point outward |
| Scalable with codebase growth | Cost scales with change size, not total codebase size |

---

*modullum — internal concept document*
