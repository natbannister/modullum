# Modullum — Scoper Module Design
*Session notes, 22 March 2026*

> **Note:** Design discussion only. Nothing implemented.

---

## Overview

The scoper is the entry point into the modullum pipeline. It receives the raw user prompt, determines the scope level of the task, produces a normalised restatement, and surfaces any assumptions it had to make to reach that determination. Everything downstream — research, requirements generation, model selection, code generation strategy — is conditional on its output.

---

## Scope Levels

The following levels were defined and tested during this session:

| Level | Label | Example Output Artefact |
|---|---|---|
| 1 | Single function | A step function returning a tuple |
| 2 | Single script, multiple functions | A simulation script outputting a dataframe |
| 3 | Multiple-script program | A program with separated modules |
| 4 | Codebase with separated concerns | A modular project with a UI layer |
| 5 | Project with documentation | Codebase plus README, API docs |
| 6 | Research project | Logging, comparative analysis, report generation |
| 7 | Research programme with longitudinal tracking | Versioned runs, self-improvement, evolving reports |

The anchor for scope determination is **output artefact required and minimum required complexity** — not the domain the task lives in. This distinction is critical: a SEIR step function is level 1 regardless of the fact that epidemiology is a research domain.

---

## Prompt Design

The following system prompt was tested and performed well with qwen3.5:9b:

```python
SYSTEM_PROMPT = ("""
What is the scope of this task?:
1. Single function coding task
2. Single script, multiple function task
3. Multiple-script program
4. Codebase generation with separated concerns
5. Project including codebase generation and documentation
6. Research project including all the above
7. Research programme, including all the above, with longitudinal tracking

Choose the closest match based on the output artefact required and minimum required complexity. Repeat the task as you understand it, and why you chose the scope level you did. DO NOT offer other explanation, implementation, or execution.
""")
```

**Key instructions and their purpose:**

- *"output artefact required"* — redirects from domain pattern-matching to deliverable reasoning
- *"minimum required complexity"* — prevents upward scope inflation on ambiguous prompts
- *"DO NOT offer other explanation, implementation, or execution"* — keeps output clean and prevents self-justifying verbosity

---

## Model Selection

| Model | Result |
|---|---|
| qwen3.5:0.8b | Failed on level 2 SEIR prompt — pattern-matched "epidemiology" → level 7. Could not hold the artefact-anchoring constraint against domain priors. |
| qwen3.5:9b | Correctly classified all five test prompts. Reasoning worked from deliverable backwards. One marginal call (level 6 vs 5) was defensible. |

The 0.8b failure is a capacity issue, not a prompt issue — the prompt is well-designed but the model cannot reliably follow the constraint when domain associations are strong. 9b is the minimum viable model for this role.

---

## Scoper Output Structure

The scoper should emit three things, not just a scope level:

1. **Scope level** — integer 1–7
2. **Task restatement** — normalised description of what is being asked, working from the deliverable. This becomes the working task definition fed to downstream modules, replacing the raw user prompt.
3. **Assumptions** — explicit list of things the scoper had to assume to land on its scope determination. These are the inputs to the research module.

The restatement is load-bearing: it normalises vague or poorly-phrased prompts at the cheapest point in the pipeline, before anything expensive runs.

---

## Role in the Pipeline

The scoper is the router for the entire system:

```
raw prompt
    ↓
scoper → [scope level + restatement + assumptions]
    ↓                           ↓
determines:             research resolves assumptions
  - whether research runs
  - requirements depth
  - model selection
  - code_gen iteration budget
  - quality signals applicable
    ↓
requirements_gen works from clean restatement
```

### What scope level gates

- **Requirements depth:** `REQUIREMENTS_CAP` and iteration budgets should scale with scope.
- **Code generation tiers:** Levels 1–3 fit in a single context. Levels 4+ require a planning pass and a reconciliation module (see below).
- **Quality signals:** pytest pass rate is meaningful for levels 1–3. Requirements coverage matters more for 4+. Longitudinal comparison only applies at 6–7.

### What scope does NOT gate:

- **Research:** Research may still be required.
- **Model selection:** The code generation module operates at the minimum scope level and requires an adequate model to function.

---

## Relationship to Assumptions and Research

There is a current overlap between assumptions in the scoper and assumptions in requirements_gen. The distinction:

- **Scoper assumptions** affect *what* is being built — if wrong, they change the scope level. Example: "I assumed this is a standalone script and not part of a larger system."
- **Requirements assumptions** affect *how* something is built — they change implementation details within a fixed scope. Example: "I assumed Python 3.10+ and a list return type."

The current requirements_gen assumption loop is likely compensating for a scoper that does not yet emit its assumptions explicitly. Once the scoper surfaces assumptions as a structured output, requirements_gen should receive a cleaner, already-resolved task definition.

The research module is the natural handler for scoper-level assumptions — things the system genuinely doesn't know that would change what gets built. The flow should be:

```
scoper emits assumptions
    ↓
research resolves assumptions (if scope warrants it)
    ↓
requirements_gen receives resolved restatement
```

Rather than research being an optional bolt-on, it becomes the handler for whatever the scoper couldn't confidently resolve.

---

## Codebase Generation at Levels 4+ (Open Problem)

Single-context code generation breaks down above level 3. The identified problems:

**Interface specification:** Files generated independently won't compose without a prior design artefact defining module boundaries, exports, and data structures passed between modules.

**Helper function standardisation:** Duplication is only visible in aggregate. File A and File B each generate their own `validate_params()` — neither is wrong in isolation. Requires either a shared utilities spec from a planning pass, or post-generation reconciliation.

**Context window constraints:** A full codebase won't fit in 9b's context. Files must be generated sequentially with a shared interface spec as anchor, or a coordinator must track global state across calls.

### Proposed reconciliation module

Generate all files independently, then run a separate reconciliation module that operates on the complete but potentially inconsistent codebase. Structured as sequential passes with a team of focused agents:

```
Pass 1: Inventory       — what exists, what each file exports and imports
Pass 2: Interface align — find mismatches between calls and definitions
Pass 3: Deduplication   — identify functionally similar code across files
Pass 4: Extraction      — pull common helpers into shared modules, update imports
Pass 5: Validation      — verify refactored codebase is internally consistent, run tests
```

Each pass has a well-defined input/output and can be a focused LLM call rather than one enormous context. Pass 1 is cheap and produces the shared manifest that everything else works from.

**Key design question:** Destructive (rewrite in place) vs additive (produce a diff/patch set applied separately). The latter is more auditable and consistent with existing provenance philosophy — raw generation output is preserved before reconciliation touches it.

**Risk:** Deduplication (pass 3–4) requires semantic understanding of function behaviour, not just syntactic similarity. A model may confidently merge two functions that look similar but differ subtly. Validation pass must run tests against refactored output, not just check imports resolve.

---

## Open Questions

- What is the minimum scope level at which research should be triggered automatically vs. conditionally?
- Should the scoper run as a single call or include a clarification loop for genuinely ambiguous prompts?
- How does the restatement handle prompts that are ambiguous between two scope levels? Does it pick one or flag the ambiguity?
- At what scope level does the planning pass (interface spec) become mandatory before code_gen runs?
- How does the reconciliation module interact with the existing test/diagnose/repair loop in code_gen?

---

## Related Session Topics (not covered here)

- Research module design and two-stage architecture
- Quality scoring and the diagnosis/repair loop in code_gen
- Longitudinal tracking and self-modification at levels 6–7
- The "when does the system not know" problem for automated research triggering