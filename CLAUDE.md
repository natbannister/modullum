# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Modullum is an agentic node-based modular task execution pipeline that runs locally using Ollama. It takes a user task, generates structured requirements through a requirements_gen module, then generates code and tests via a code_gen module.

## Running

```bash
# Activate virtual environment
source .venv/bin/activate

# Run the agent (prompts for task input)
modullum
```

## Architecture

```
modullum/
├── main.py              # Entry point. Creates RunContext, logger, and HeadAgent
├── config.py             # Global flags: model selection, streaming, iteration caps, token limits
├── agents/
│   └── head.py           # HeadAgent orchestrates the pipeline (requirements_gen → code_gen)
├── core/
│   ├── nodes.py          # Node class (conversation history), call_node() (Ollama chat), schema utilities
│   ├── workspace.py       # RunContext (top-level run dirs, git info, finalization), ModuleContext (per-module records)
│   └── terminal.py       # StreamingConsoleHandler for token-by-token terminal output, setup_logger()
└── modules/
    ├── requirements_gen.py  # Interview → assumptions → requirements generation with user feedback loops
    ├── code_gen.py          # Test generation → code generation → pytest loop with diagnosis/repair
    └── scope_manager.py     # (currently not called from head.py)
```

**Pipeline flow** (in `head.py`):
1. `requirements_gen.run()` returns a `RequirementsList` (Pydantic model with task + requirements)
2. `code_gen.run()` takes the requirements, generates tests, generates code, runs pytest, repairs on failure

**Node system**: A `Node` holds a system prompt and conversation history. `call_node()` sends it to Ollama with optional JSON schema enforcement and streaming. Structured output is parsed via Pydantic validation with truncated JSON salvage.

**Run artifacts** (`runs/{serial}/`):
- `run_manifest.json` — full run metadata, timing, module metrics
- `version_record.csv` — append-only run log across all runs
- `{module}/transcript.jsonl` — per-node records
- `{module}/prompts.json` — prompt library by hash
- `outputs/{code,tests}.py` — generated artifacts
- `configs/` — hashed config snapshots
- `diffs/` — git diffs at run time

## Configuration

All toggles are in `config.py`:
- `MODEL` — Ollama model (default: `granite3.1-moe:3b`)
- `STREAM_*` flags — control terminal streaming per node type
- `USER_PROMPT`, `AUTO_SKIP`, `INTERVIEW` — user interaction modes
- `MAX_*_ITERATIONS` — iteration caps for test/code generation loops
- `INPUT_REVIEW`, `TESTS_FEEDBACK` — feedback analysis before generation

## Key Patterns

- `ctx.module("name")` returns a `ModuleContext` for a named module
- `ctx.start_node()` → `call_node()` → `record.finish()` → `ctx.record_node()` — the node recording pattern
- `schema_to_prompt_hint(schema)` — converts Pydantic JSON schema to human-readable prompt text
- `flatten_schema(schema)` — resolves `$ref` in JSON schemas for Ollama compatibility
