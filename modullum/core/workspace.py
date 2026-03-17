"""
workspace.py — centralised run context, recording, and directory management.

Architecture:
    RunContext        owns a run: serial, paths, git info, config snapshot, final flush
    └── ModuleContext owns a module's subdirectory and node records
        └── NodeRecord dataclass captures a single node call

Usage (in head.py):
    ctx = RunContext(base_dir)
    requirements_json = requirements_gen.run(ctx.module("requirements_gen"), logger)
    code, tests = code_gen.run(ctx.module("code_gen"), logger, requirements_json)
    ctx.finalise(task=initial_prompt, exit_reason="completed")

Usage (in a module):
    node_rec = ctx.start_node(
        role="generator",
        prompt=GENERATOR_PROMPT,
        model=config.MODEL,
        stream=config.STREAM_REQUIREMENTS_GEN,
        think=config.REQUIREMENTS_GEN_THINK,
        temperature=config.TEMPERATURE,
    )
    # ... call_node(...) ...
    node_rec.finish(
        tokens_in=response.prompt_eval_count,
        tokens_out=response.eval_count,
        llm_duration_s=elapsed,
        iterations=n,
        exit_reason="accepted",
        output=str(result),
    )
    ctx.record_node(node_rec)
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _write_if_new(library_dir: Path, hash_str: str, content: str, suffix: str) -> None:
    """Write content to library_dir/{hash}.{suffix} only if it doesn't exist yet."""
    library_dir.mkdir(parents=True, exist_ok=True)
    target = library_dir / f"{hash_str}{suffix}"
    if not target.exists():
        target.write_text(content, encoding="utf-8")


def _git_info(base_dir: Path, diff_library: Path) -> dict:
    """
    Returns commit hash, diff_hash (sha256[:12] of the full diff, or None if
    clean), and list of modified files.  The raw diff is written to
    diff_library/{diff_hash}.diff so it can be replayed later.
    Falls back gracefully if git is unavailable or base_dir is not a repo.
    """
    def run(cmd: list[str]) -> str:
        result = subprocess.run(
            cmd, cwd=base_dir, capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    commit = run(["git", "rev-parse", "--short", "HEAD"])
    if not commit:
        return {"commit": None, "diff_hash": None, "dirty_files": []}

    diff_text = run(["git", "diff", "HEAD"])
    if not diff_text:
        return {"commit": commit, "diff_hash": None, "dirty_files": []}

    diff_hash = _sha256_text(diff_text)
    _write_if_new(diff_library, diff_hash, diff_text, ".diff")

    dirty_files = run(["git", "diff", "--name-only", "HEAD"]).splitlines()

    return {
        "commit": commit,
        "diff_hash": diff_hash,
        "dirty_files": dirty_files,
    }


def _config_snapshot(base_dir: Path, library_dir: Path) -> dict:
    """
    Reads config.py, hashes it, writes to library if new.
    Returns {"hash": ..., "snapshot": {field: value, ...}}.
    """
    config_path = base_dir / "modullum" / "config.py"
    if not config_path.exists():
        config_path = base_dir / "config.py"   # flat layout fallback

    if not config_path.exists():
        return {"hash": None, "snapshot": {}}

    raw = config_path.read_text(encoding="utf-8")
    h = _sha256_text(raw)
    _write_if_new(library_dir, h, raw, ".py")

    # Parse the public, non-dunder names as a lightweight snapshot
    namespace: dict = {}
    try:
        exec(compile(raw, config_path.name, "exec"), namespace)  # nosec — own config file
    except Exception:
        pass
    snapshot = {
        k: v for k, v in namespace.items()
        if not k.startswith("_") and isinstance(v, (str, int, float, bool, list, set, dict, type(None)))
    }
    # sets aren't JSON-serialisable
    snapshot = {k: (list(v) if isinstance(v, set) else v) for k, v in snapshot.items()}

    return {"hash": h, "snapshot": snapshot}


# ── NodeRecord ────────────────────────────────────────────────────────────────

@dataclass
class NodeRecord:
    """Records a single node call. Call finish() after call_node() returns."""

    # Identity
    module: str
    role: str                  # e.g. "interviewer", "generator", "repairer"
    prompt_hash: str           # sha256[:12] of system prompt
    prompt_text: str           # full system prompt (inline for human traceability)

    # Config at call time
    model: str
    stream: bool
    think: bool
    temperature: float

    # Filled by finish()
    iterations: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_duration_s: float = 0.0
    exit_reason: str = ""      # "accepted" | "auto_skip" | "cap_reached" | "error"
    output: str = ""           # final assistant response
    error: str | None = None   # exception string if the node errored

    # Wall-clock bookkeeping (set internally)
    _wall_start: float = field(default=0.0, repr=False)
    wall_duration_s: float = 0.0
    user_wait_s: float = 0.0   # wall - llm

    def finish(
        self,
        tokens_in: int,
        tokens_out: int,
        llm_duration_s: float,
        iterations: int,
        exit_reason: str,
        output: str,
        error: str | None = None,
    ) -> None:
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.llm_duration_s = round(llm_duration_s, 3)
        self.iterations = iterations
        self.exit_reason = exit_reason
        self.output = output
        self.error = error
        self.wall_duration_s = round(time.monotonic() - self._wall_start, 3)
        self.user_wait_s = round(self.wall_duration_s - self.llm_duration_s, 3) # TODO: Make this actually time due to waiting for user input

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_wall_start", None)
        return d


def start_node(
    module: str,
    role: str,
    prompt: str,
    model: str,
    stream: bool,
    think: bool,
    temperature: float,
) -> NodeRecord:
    """
    Convenience factory. Call this before call_node(), pass the result to
    record.finish() afterwards, then hand it to ModuleContext.record_node().
    """
    h = _sha256_text(prompt)
    rec = NodeRecord(
        module=module,
        role=role,
        prompt_hash=h,
        prompt_text=prompt,
        model=model,
        stream=stream,
        think=think,
        temperature=temperature,
    )
    rec._wall_start = time.monotonic()
    return rec


# ── ModuleContext ─────────────────────────────────────────────────────────────

class ModuleContext:
    """
    Owned by a module. Accumulates NodeRecords and flushes them to disk.

    Directory layout:
        runs/{serial}/{module_name}/
            prompts.json        — all unique prompts used, keyed by hash
            transcript.jsonl    — chronological node records (one JSON object per line)
            metrics.json        — aggregated metrics for this module run
    """

    def __init__(
        self,
        module_name: str,
        module_dir: Path,
        prompt_library: Path,
        run_start: float,
    ):
        self.module_name = module_name
        self.module_dir = module_dir
        self.prompt_library = prompt_library
        self._run_start = run_start
        self._records: list[NodeRecord] = []
        self.exit_reason: str = "incomplete"
        self.error: str | None = None
        self.quality_score: float | None = None   # populated later, per-module

        module_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def start_node(
        self,
        role: str,
        prompt: str,
        model: str,
        stream: bool = False,
        think: bool = False,
        temperature: float = 0.0,
    ) -> NodeRecord:
        """Create and return a NodeRecord. Call .finish() on it after call_node()."""
        return start_node(
            module=self.module_name,
            role=role,
            prompt=prompt,
            model=model,
            stream=stream,
            think=think,
            temperature=temperature,
        )

    def record_node(self, record: NodeRecord) -> None:
        """Append a finished NodeRecord. Also writes its prompt to the shared library."""
        _write_if_new(self.prompt_library, record.prompt_hash, record.prompt_text, ".txt")
        self._records.append(record)

    def set_outcome(
        self,
        exit_reason: str,
        error: str | None = None,
        quality_score: float | None = None,
    ) -> None:
        self.exit_reason = exit_reason
        self.error = error
        self.quality_score = quality_score

    def flush(self, outputs: dict[str, Path] | None = None) -> dict:
        """
        Write prompts.json, transcript.jsonl, metrics.json.
        Returns the metrics dict for inclusion in the run manifest.

        outputs: optional dict of {label: Path} for files written by this module,
                 e.g. {"requirements": path_to_requirements_txt}
        """
        # ── prompts.json ──────────────────────────────────────────────────────
        prompts: dict[str, str] = {}
        for r in self._records:
            if r.prompt_hash not in prompts:
                prompts[r.prompt_hash] = r.prompt_text

        prompts_file = self.module_dir / "prompts.json"
        prompts_file.write_text(json.dumps(prompts, indent=2), encoding="utf-8")

        # ── transcript.jsonl ──────────────────────────────────────────────────
        transcript_file = self.module_dir / "transcript.jsonl"
        with transcript_file.open("w", encoding="utf-8") as f:
            for r in self._records:
                f.write(json.dumps(r.to_dict()) + "\n")

        # ── metrics.json ──────────────────────────────────────────────────────
        metrics = self._aggregate_metrics(outputs)
        metrics_file = self.module_dir / "metrics.json"
        metrics_file.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

        return metrics

    # ── Internal ──────────────────────────────────────────────────────────────

    def _aggregate_metrics(self, outputs: dict[str, Path] | None) -> dict:
        total_llm = round(sum(r.llm_duration_s for r in self._records), 3)
        total_wall = round(time.monotonic() - self._run_start, 3)
        total_user_wait = round(sum(r.user_wait_s for r in self._records), 3)

        nodes_summary = []
        for r in self._records:
            nodes_summary.append({
                "role": r.role,
                "prompt_hash": r.prompt_hash,
                "model": r.model,
                "iterations": r.iterations,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "llm_duration_s": r.llm_duration_s,
                "wall_duration_s": r.wall_duration_s,
                "user_wait_s": r.user_wait_s,
                "stream": r.stream,
                "think": r.think,
                "temperature": r.temperature,
                "exit_reason": r.exit_reason,
                "error": r.error,
            })

        return {
            "module": self.module_name,
            "exit_reason": self.exit_reason,
            "quality_score": self.quality_score,
            "error": self.error,
            "total_node_calls": len(self._records),
            "total_tokens_in": sum(r.tokens_in for r in self._records),
            "total_tokens_out": sum(r.tokens_out for r in self._records),
            "llm_duration_s": total_llm,
            "total_duration_s": total_wall,
            "user_wait_s": total_user_wait,
            "outputs": {k: str(v) for k, v in (outputs or {}).items()},
            "nodes": nodes_summary,
        }


# ── RunContext ────────────────────────────────────────────────────────────────

class RunContext:
    """
    Top-level context for a single run. Owns serial number, directory structure,
    git info, config snapshot, and final flush to run_manifest.json and
    version_record.csv.

    Usage:
        ctx = RunContext(base_dir)
        mod_ctx = ctx.module("requirements_gen")   # returns ModuleContext
        ...
        ctx.finalise(task="Create a SEIR model", exit_reason="completed")
    """

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self._wall_start = time.monotonic()
        self._start_dt = datetime.now()

        runs_dir = base_dir / "runs"
        runs_dir.mkdir(exist_ok=True)

        # ── Serial ────────────────────────────────────────────────────────────
        existing = [p.name for p in runs_dir.iterdir() if p.is_dir() and p.name.isdigit()]
        self.serial = max((int(p) for p in existing), default=0) + 1

        # ── Directories ───────────────────────────────────────────────────────
        self.run_dir = runs_dir / str(self.serial)
        self.outputs_dir = self.run_dir / "outputs"
        self._prompt_library = runs_dir / "prompts"
        self._config_library = runs_dir / "configs"
        self._diff_library   = runs_dir / "diffs"
        self._version_csv = runs_dir / "version_record.csv"

        self.run_dir.mkdir()
        self.outputs_dir.mkdir()

        # ── Git + config ──────────────────────────────────────────────────────
        self._git = _git_info(base_dir, self._diff_library)
        self._config = _config_snapshot(base_dir, self._config_library)

        # ── Module contexts ───────────────────────────────────────────────────
        self._modules: dict[str, ModuleContext] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def module(self, name: str) -> ModuleContext:
        """Return (creating if needed) the ModuleContext for a named module."""
        if name not in self._modules:
            self._modules[name] = ModuleContext(
                module_name=name,
                module_dir=self.run_dir / name,
                prompt_library=self._prompt_library,
                run_start=time.monotonic(),
            )
        return self._modules[name]

    def finalise(
        self,
        task: str,
        exit_reason: str = "completed",
        notes: str = "",
    ) -> None:
        """
        Flush all module contexts, write run_manifest.json, append version_record.csv.
        Call this from head.py in both the happy path and the except/finally block.
        """
        wall_duration = round(time.monotonic() - self._wall_start, 3)
        llm_duration = 0.0
        total_node_calls = 0
        module_metrics: dict[str, dict] = {}

        for name, mod_ctx in self._modules.items():
            metrics = mod_ctx.flush()
            module_metrics[name] = metrics
            llm_duration += metrics["llm_duration_s"]
            total_node_calls += metrics["total_node_calls"]

        llm_duration = round(llm_duration, 3)

        # ── run_manifest.json ─────────────────────────────────────────────────
        manifest = {
            "serial": self.serial,
            "timestamp": self._start_dt.isoformat(),
            "task": task,
            "exit_reason": exit_reason,
            "notes": notes,
            "git": self._git,
            "config": self._config,
            "timing": {
                "llm_duration_s": llm_duration,
                "total_duration_s": wall_duration,
                "user_wait_s": round(wall_duration - llm_duration, 3),
            },
            "total_node_calls": total_node_calls,
            "quality_score": None,   # populated later by evaluation module
            "modules": module_metrics,
        }

        manifest_path = self.run_dir / "run_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # ── version_record.csv ────────────────────────────────────────────────
        _CSV_FIELDS = [
            "serial", "timestamp", "task_summary", "git_hash", "diff_hash",
            "config_hash", "config_alias", "model", "total_node_calls",
            "llm_duration_s", "total_duration_s", "exit_reason",
            "quality_score", "notes",
        ]
        write_header = not self._version_csv.exists()
        with self._version_csv.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow({
                "serial": self.serial,
                "timestamp": self._start_dt.isoformat(),
                "task_summary": task[:120],
                "git_hash": self._git.get("commit"),
                "diff_hash": self._git.get("diff_hash"),
                "config_hash": self._config.get("hash"),
                "config_alias": None,   # set manually or by a future naming layer
                "model": self._config["snapshot"].get("MODEL"),
                "total_node_calls": total_node_calls,
                "llm_duration_s": llm_duration,
                "total_duration_s": wall_duration,
                "exit_reason": exit_reason,
                "quality_score": None,
                "notes": notes,
            })