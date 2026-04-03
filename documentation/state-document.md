# modullum — Project State Document

*Last updated: session ending March 2026*

---

## Vision

Modullum is a local-first agentic code generation system designed to produce high-quality, requirements-verified code through structured internal workflows rather than high-context feedback loops. The long-horizon goal is a self-improving system: one that analyses its own run history, identifies failure patterns, and adjusts configuration, prompts, and module behaviour to converge faster and more reliably over time.

The system runs on consumer hardware (M1 Pro, 16GB). Cloud LLM calls are a last resort. All core operation is local and deterministic where possible; LLM calls are narrow, scoped, and targeted.

---

## Architecture

### Three-tier hierarchy

```
Orchestrator (HeadAgent)
    └── Modules  (requirements_gen, code_gen, scope_manager, ...)
            └── Nodes  (individual call_node() invocations with defined roles)
```

The head agent sequences modules and owns the run context. Modules own their internal node loops and self-validate before returning. Nodes are single LLM calls with tightly scoped context.

### Core principles

- **Locality** — all inference runs locally via Ollama; no external API dependency
- **Determinism** — graph traversal and structured lookups preferred over model inference for any decision that can be made programmatically
- **Narrow context** — each node receives exactly the slice it needs; the retrieval and assembly layer is smarter than the models
- **Propose before executing** — the system surfaces work packages for user approval; execution is never automatic
- **Cost scales with change size, not codebase size** — NCR detection and context assembly are scoped to the neighbourhood of a change

### Known constraint
Local models (tested: Granite, Qwen variants via Ollama) are not reliable for agentic tool-calling under frontier-model-style system prompts. Architecture does not depend on this capability.

---

## Current Implementation State

### What exists and works

**`modullum/core/`**
- `nodes.py` — `Node` class (conversation history manager), `call_node()` wrapper returning `NodeResult(output, tokens_in, tokens_out, llm_duration_s)`, JSON schema enforcement, streaming with thinking block support, truncation salvage
- `workspace.py` — `RunContext`, `ModuleContext`, `NodeRecord`; full recording infrastructure (see below)
- `terminal.py` — Rich/prompt_toolkit terminal utilities, `status_spinner`
- `stopwatch.py` — superseded by `NodeRecord` timing; retained

**`modullum/modules/`**
- `requirements_gen.py` — interview, assumptions review, iterative requirements generation with user feedback loop; outputs `requirements.txt`
- `code_gen.py` — test generation, optional test feedback, code generation, diagnosis and repair loop (code and test sides independently); outputs `code.py`, `tests.py`
- `scope_manager.py` — stub; scope classification not yet implemented

**`modullum/agents/`**
- `head.py` — sequences modules, handles keyboard interrupt and exceptions, guarantees manifest flush via `finally`

**`main.py`** — constructs `RunContext`, sets up logger pointed at run directory, instantiates and runs `HeadAgent`

### What is stubbed or incomplete
- `scope_manager` — interface defined, no implementation
- Requirements pipeline currently bypassed in `head.py` (`TEMP_REQUIREMENTS` hardcoded in `code_gen`)
- No persistent storage (SQLite, vector DB, graph DB) — all planned, none implemented
- No NCR detection pipeline
- No context assembler
- No TUI (Rich/Textual planned for early dev; web UI longer horizon)
- No analysis pipelines (run summariser, grapher, report writer)

---

## Recording Infrastructure

Built in the most recent session. Every run produces a fully structured record at `runs/{serial}/`.

### Directory layout
```
runs/
  prompts/                      — content-addressed prompt library ({hash}.txt)
  configs/                      — content-addressed config snapshots ({hash}.py)
  version_record.csv            — lightweight human-scannable index
  {serial}/
    run_manifest.json           — full structured run record
    run.log                     — logger output
    requirements_gen/
      prompts.json
      transcript.jsonl          — chronological node records
      metrics.json
    code_gen/
      prompts.json
      transcript.jsonl
      metrics.json
    outputs/
      requirements.txt
      code.py
      tests.py
```

### What is captured per run
- Git commit hash, dirty flag, list of modified files
- Full config snapshot (all fields, hashed for comparability)
- Task verbatim, exit reason, notes
- Per module: total node calls, tokens in/out, `llm_duration_s`, `total_duration_s`, `user_wait_s`, `script_duration_s`, exit reason, quality score (null)
- Per node: role, prompt hash + full text, model, stream/think/temperature, iterations, tokens in/out, all four timing fields, exit reason, output, error

### Timing model
Four fields, each with distinct semantics:
- `llm_duration_s` — blocking `ollama.chat()` time only
- `user_wait_s` — accumulated at `get_input()` call sites via `ctx.add_user_wait()`
- `script_duration_s` — derived: `total - llm - user_wait`; covers inter-node gaps, JSON parsing, subprocess calls (pytest), node construction
- `total_duration_s` — wall clock from `ModuleContext` construction to first `flush()`; frozen on first flush, returned from cache on subsequent calls

### Node roles currently recorded
| Module | Role | Condition |
|---|---|---|
| `requirements_gen` | `interviewer` | `INTERVIEW=True` |
| `requirements_gen` | `assumptions` | `ASSUMPTIONS_USER_REVIEW=True` |
| `requirements_gen` | `generator` | always |
| `code_gen` | `test_generator` | always |
| `code_gen` | `test_feedback` | `TESTS_FEEDBACK=True` |
| `code_gen` | `code_generator` | always |
| `code_gen` | `diagnosis` | per repair cycle |
| `code_gen` | `code_repairer` | per repair dispatch |
| `code_gen` | `test_repairer` | per repair dispatch |

---

## Planned: Self-Improvement Infrastructure

The recording infrastructure is the foundation. The next initiative builds on it in two phases:

### Phase 1 — Analysis pipelines
Supplementary pipelines invocable via `main.py` or the head agent as a dispatcher:
- **Run summariser** — reads `transcript.jsonl`, produces human-readable narrative of what the model did and where it struggled
- **Run analyser** — compares runs across config hashes; identifies convergence patterns, failure modes, token cost trends
- **Report writer** — structured report from `metrics.json` across a set of runs
- **Grapher** — visualisations from run metrics

All analysis modules check an `analysis_index.json` at `runs/` level to identify unprocessed serials, work through them in batch, and write back. The head agent dispatcher becomes: load index → find gaps → dispatch.

### Phase 2 — Knowledge base and convergence records
- Vector store over transcripts and run summaries for semantic retrieval
- Structured convergence records: which prompt variants, config combinations, and node layouts converge faster and more reliably
- Evaluation module writing back into `quality_score` — initially human-calibrated, eventually model-driven
- Config and prompt tuning suggestions derived from convergence analysis

The key insight: `config_hash` and `git_hash` together mean the system can distinguish "this configuration converged faster" from "this code change converged faster" — a meaningful difference for targeted self-improvement.

---

## Planned: Full Pipeline Architecture

### Storage (not yet implemented)
Three complementary stores, each suited to a different retrieval pattern:
- **SQLite** — structured exact lookups: requirements, work packages, NCR records, test results, interface definitions
- **Vector DB** (ChromaDB or LanceDB, undecided) — semantic similarity: historical NCRs, related requirements, past review summaries
- **Graph DB** (SQLite adjacency or NetworkX) — deterministic relationship traversal: requirement traceability, file dependencies, impact chains

### Context assembly (not yet implemented)
Per-node context templates assembled deterministically from the three stores. Scope level (function → script → module → codebase → project) drives context depth. Condensation strategies: hierarchical summarisation, diff-based context, anchored truncation.

### NCR detection (not yet implemented)
Change-triggered, not global. Detection pipeline: graph lookup → requirement-to-code mapping → structural pre-checks (no model) → targeted LLM review (scoped to 2–4 requirements × 1 file). Cost scales with change size, not codebase size.

### Scope manager (not yet implemented)
Six-level blast radius classification: function, script, module, codebase, project, longitudinal. Classification by graph traversal + requirement hierarchy mapping, not model inference. Interface boundary check can escalate scope one level.

### UI
- **Near term** — Rich/Textual TUI; htop aesthetic, panelled, keyboard-navigable
- **Longer horizon** — web UI once proposal artefact format stabilises; same information hierarchy, richer rendering

---

## Hardware and Tooling

| Item | Detail |
|---|---|
| Hardware | MacBook Pro M1 Pro, 16GB RAM |
| Language | Python |
| Local inference | Ollama |
| Models tested | `granite3.1-moe:3b`, `qwen2.5-coder`, `qwen3.5:9b`, `qwen3.5:0.8b` |
| TUI (planned) | Rich / Textual |
| Vector DB (candidates) | ChromaDB, LanceDB |
| Requirements structure | ECSS-derived MBSE, directory-based artefacts with frontmatter metadata |

---

*modullum — internal state document*