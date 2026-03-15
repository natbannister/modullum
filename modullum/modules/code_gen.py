import csv
import logging
import re
import subprocess
import tempfile
import sys
from datetime import datetime
from pathlib import Path
from pydantic import Field

from enum import Enum
from pydantic import BaseModel
from prompt_toolkit import prompt
from prompt_toolkit.styles import Style

from modullum.core import Node, call_node, schema_to_prompt_hint, Stopwatch, status_spinner
from modullum import config


# ── Prompt toolkit style ──────────────────────────────────────────────────────

_style = Style.from_dict({"placeholder": "#666666"})


def get_input(placeholder: str = "Send a message") -> str:
    return prompt(">>> ", placeholder=placeholder, style=_style)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class FailedNode(str, Enum):

    # NOTE: Currently not implemented due to previous JSON non-adherence but MAY work now JSON more strictly enforced.
    # NOTE: If re-implementing, insert into failed_node in DiagnosedFix (below).

    code = "code"
    tests = "tests"

    def __str__(self):
        return self.value


class DiagnosedFix(BaseModel):
    #target_test: str = Field(description="The name of the failing test")
    #failed_node: FailedNode = Field(description="'tests' or 'code'")
    failed_node: str = Field(description="'tests' or 'code'")
    #diagnosis: str
    fix: str = Field(description="Plain text description of the fix")
    code_snippet: str | None = Field(default=None, description="Optional illustrative code snippet for the fix")

class Diagnosis(BaseModel):
    fixes: list[DiagnosedFix]

    def __str__(self):
        return "\n\n".join(
            f"[{f.failed_node}]\nFix: {f.fix}"
            + (f"\n```python\n{f.code_snippet}\n```" if f.code_snippet else "")
            for f in self.fixes
        )

class TestReview(BaseModel):
    test_name: str
    requirement_id: str
    conformance: bool
    reason: str
    amendment: str | None = None

    def __str__(self):
        return f"[Test: {self.test_name}][Requirement: {self.requirement_id}][Conformance: {self.conformance}]\n[Reason: {self.reason}]\n[Amendment: {self.amendment}]"


class ManagerAction(BaseModel):
    tests_review_list: list[TestReview]
    approved: bool = Field(description="True if all tests conform to requirements, False if any amendments are needed")

    def __str__(self):
        reviews = "\n".join(str(r) for r in self.tests_review_list)
        return f"{reviews}\nApproved: {self.approved}"


class Requirement(BaseModel):
    serial: int
    type: str
    req: str

    def __str__(self):
        return f"[{self.serial}][{self.type}] - {self.req}"


class RequirementsList(BaseModel):
    reqs: list[Requirement]

    def __str__(self):
        return "\n".join(str(r) for r in self.reqs)


class ModuleOutput(BaseModel):
    code: str
    tests: str
    max_test_iterations: int
    max_code_iterations: int
    test_generation_iterations: int
    code_generation_iterations: int
    test_generation_time: float
    code_generation_time: float
    function_time: float
    passed: bool


# ── CSV fields ────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "Timestamp",
    "Script",
#    "Task",
    "Serial",
    "Model",
    "Test Generation Iterations",
    "Code Generation Iterations",
    "Test Generation Duration",
    "Code Generation Duration",
    "Total Runtime",
    "LLM Time",
    "Passed",
    "Notes",
]


# ── Prompt constants ──────────────────────────────────────────────────────────

TEST_GENERATOR_PROMPT = (
    "Generate pytest tests only. No explanation. Always start your output with 'import pytest'. Do not output anything other than Python code."
    "\nAlways import the function using: from module import <function_name>. Generate one test per functional requirement."
    "\nNever implement or redefine the function in the test file. The function will be provided separately."
    "\nDo not generate tests that check function signatures or parameter counts."
    f"\nInclude a comment at the start: # Generated in Modullum by {config.MODEL}"
)

FEEDBACK_PROMPT = (
    "Use the requirements list to determine whether each test provided (1) correctly identifies whether the "
    "requirement(s) will be satisfied by a separate function designed to meet the requirement, (2) does not "
    "contain any syntactical or logical errors and (3) is not vacuous."
    "\n Check for argument position errors."
    "\n Tests are approved once all are conformant."
    f"\n{schema_to_prompt_hint(ManagerAction)}"
)

CODE_GENERATOR_PROMPT = (
    "Use the requirements to generate Python code only. No explanation."
    f"\nInclude a comment at the start: # Generated in Modullum by {config.MODEL}"
)

DIAGNOSIS_PROMPT = (
    "You will receive a set of requirements, the code generated from those requirements, and the results from "
    "unit tests running the code. Analyse the failures and populate the 'fixes' list — one entry per failing test."
    "\nIf pytest failed during collection (0 items collected), the error is in the tests, not the code. Fix the error in the tests file."
    "\nCheck tests conform to the requirements before assuming the code is at fault." if not config.TESTS_FEEDBACK else ""
    "\nOnly diagnose issues that directly cause test failures. Ignore style, formatting, and cosmetic issues."
    f"\n{schema_to_prompt_hint(Diagnosis)}"
)

# TEMPORARY REQUIREMENTS TO SPEED UP DEVELOPMENT:
TEMP_REQUIREMENTS = """
[REQ-001][function_signature][high][1.0] - The function must be named `seir_step` and accept arguments for current susceptible (S), exposed (E), infected (I), recovered (R) populations, transmission rate (beta), progression rate (sigma), recovery rate (gamma), and time step size (dt).
[REQ-002][functional_behavior][high][1.0] - The function must implement the standard SEIR differential equations: dS/dt = -beta*S*I/N, dE/dt = beta*S*I/N - sigma*E, dI/dt = sigma*E - gamma*I, dR/dt = gamma*I.
[REQ-003][functional_behavior][high][1.0] - The function must return a dictionary or object containing the updated values for S, E, I, and R after one time step.
[REQ-004][boundary_cases][medium][1.0] - If the total population N (S+E+I+R) is zero, the function must return the current state unchanged without raising an error.
[REQ-005][boundary_cases][high][1.0] - If any of the population compartments (S, E, I, R) are negative upon entry, the function must raise a `ValueError`.
[REQ-006][constraints][high][1.0] - The time step `dt` must be a positive number; if non-positive, the function must raise a `ValueError`.
[REQ-007][constraints][high][1.0] - The rates beta, sigma, and gamma must be non-negative numbers; if negative, the function must raise a `ValueError`.
[REQ-008][functional_behavior][medium][1.0] - The implementation must use an explicit Euler method or a specified numerical integration scheme (e.g., RK4) if a parameter `method` is provided.
[REQ-009][interface][high][1.0] - The function must accept population values as either integers or floats and return updated values of the same numeric type.
[REQ-010][functional_behavior][medium][0.9] - If a `method` argument is provided and set to 'RK4', the function must implement the fourth-order Runge-Kutta algorithm for the SEIR system.
[REQ-011][constraints][medium][0.8] - The function must not modify the input arguments directly if they are immutable types (e.g., tuples), but may accept mutable lists or dicts.
[REQ-012][interface][high][1.0] - The function must include a docstring describing the SEIR model parameters, their units (implicit or explicit), and the expected return structure.
[REQ-013][functional_behavior][medium][0.9] - The function must handle floating-point precision errors by ensuring the sum of S, E, I, R remains numerically close to N (within a small epsilon).
[REQ-014][constraints][high][1.0] - The function must be compatible with standard scientific Python environments (e.g., NumPy arrays for vectorized inputs if applicable).
[REQ-015][functional_behavior][medium][0.8] - If the `dt` is extremely small (below a threshold like 1e-9), the function should issue a warning or clamp the step to prevent numerical instability.
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def run_tests(code: str, tests: str) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(f"{tmpdir}/module.py", "w") as f:
            f.write(code)
        with open(f"{tmpdir}/test_module.py", "w") as f:
            f.write(re.sub(r'from \w+ import', 'from module import', tests))

        result = subprocess.run(
            ["python3", "-m", "pytest", f"{tmpdir}/test_module.py", "-v"],
            capture_output=True, text=True,
            cwd=tmpdir,
        )

        return {
            "passed": result.returncode == 0,
            "output": result.stdout + result.stderr,
        }


def _format_fixes(fixes: list[DiagnosedFix]) -> str:
    """Renders a list of DiagnosedFix objects into a concise prompt-ready string."""
    return "\n\n".join(
        f"[{f.failed_node}] {f.fix}"
        + (f"\n```python\n{f.code_snippet}\n```" if f.code_snippet else "")
        for f in fixes
    )


def _apply_code_fixes(
    code: str,
    requirements: str,
    fixes: list[DiagnosedFix],
) -> tuple[Node, str]:
    """
    Spawns a fresh code repair node, calls it, and returns (node, repaired_code).

    The node is given only what it needs: the requirements, the current code,
    and the specific fixes to apply — no accumulated diagnosis history.
    """
    repair_node = Node(CODE_GENERATOR_PROMPT)
    repair_node.add_user(
        f"Requirements:\n{requirements}\n\n"
        f"Current code:\n{code}\n\n"
        f"Apply the following fixes, do NOT change any other code:\n{_format_fixes(fixes)}"
    )
    repaired = call_node(repair_node, stream=config.STREAM_CODE)
    repair_node.add_assistant(repaired)
    return repair_node, repaired


def _apply_test_fixes(
    tests: str,
    requirements: str,
    fixes: list[DiagnosedFix],
) -> tuple[Node, str]:
    """
    Spawns a fresh test repair node, calls it, and returns (node, repaired_tests).

    The node is given only what it needs: the requirements, the current tests,
    and the specific fixes to apply — no accumulated diagnosis history.
    """
    repair_node = Node(TEST_GENERATOR_PROMPT)
    repair_node.add_user(
        f"Requirements:\n{requirements}\n\n"
        f"Current tests:\n{tests}\n\n"
        f"Apply the following fixes, do NOT change any other code:\n{_format_fixes(fixes)}"
    )
    repaired = call_node(repair_node, stream=config.STREAM_CODE)
    repair_node.add_assistant(repaired)
    return repair_node, repaired


def _dispatch_fixes(
    diagnosis: Diagnosis,
    code: str,
    tests: str,
    requirements: str,
    logger: logging.Logger,
) -> tuple[str, str]:
    """
    Partitions fixes by target node, spawns a lightweight repair node for each
    affected side, and returns the (possibly updated) code and tests.

    Both sides can be repaired in the same iteration if the diagnosis targets both.
    """
    # Revert to using FailedNode in the future if the code/tests flagging doesn't work
    code_fixes = [f for f in diagnosis.fixes if f.failed_node == "code"]
    test_fixes = [f for f in diagnosis.fixes if f.failed_node == "tests"]

    if code_fixes:
        logger.info(f"\n  Applying {len(code_fixes)} code fix(es)...")
        _, code = _apply_code_fixes(code, requirements, code_fixes)

    if test_fixes:
        logger.info(f"\n  Applying {len(test_fixes)} test fix(es)...")
        _, tests = _apply_test_fixes(tests, requirements, test_fixes)

    if not code_fixes and not test_fixes:
        logger.info("  Diagnosis produced no actionable fixes.")

    return code, tests


# ── Main entry point ──────────────────────────────────────────────────────────

def run(base_dir: Path, logger: logging.Logger, requirements: str) -> Path:
    """
    Runs the code generation module.

    Args:
        base_dir:     Project root (used to locate/create the runs/ directory).
        logger:       Logger instance from main.py.
        requirements: Requirements string passed in from requirements_gen.

    Returns:
        Path to the saved output_code.py file.
    """
    timer = Stopwatch()        # accumulates pure LLM call time across both phases
    phase_timer = Stopwatch()  # tracks wall-clock time per phase (and total)

    # ── Build nodes ───────────────────────────────────────────────────────────
    test_node = Node(TEST_GENERATOR_PROMPT)
    feedback_node = Node(FEEDBACK_PROMPT)
    code_node = Node(CODE_GENERATOR_PROMPT)
    diagnosis_node = Node(DIAGNOSIS_PROMPT)

    # ── Test generation ───────────────────────────────────────────────────────
    logger.info("\nGenerating unit tests...\n")

    last_tests = ""
    test_feedback = ""
    criteria_approved = False
    test_generation_iterations = 0

    phase_timer.start()

    # ====================== REMOVE this once pipeline works
    requirements = TEMP_REQUIREMENTS

    for iteration in range(config.MAX_TEST_ITERATIONS):

        # Increase token limit with each iteration so it doesn't truncate on subsequent cycles
        token_limit = config.BIG_TOKEN_LIMIT + (test_generation_iterations * config.TOKEN_LIMIT)

        logger.info(f"\n--- Test Iteration {iteration + 1} ---\n")

        if last_tests:
            test_node.add_assistant(f"Previous tests generated:\n{last_tests}")
            test_node.add_assistant(f"Feedback on previous tests:\n{test_feedback}")

        test_node.add_user(f"Requirements:\n{requirements}")
        timer.start()
        tests = call_node(
            test_node,
            stream=config.STREAM_CODE,
            token_limit=token_limit,
        )
        timer.stop()

        if tests:
            last_tests = tests
            logger.info("\nTests generated.")
            test_generation_iterations = iteration + 1

        if config.TESTS_FEEDBACK:
            feedback_node.add_user(f"Requirements:\n{requirements}\nTests:\n{tests}")
            timer.start()
            test_feedback = call_node(
                feedback_node,
                ManagerAction,
                stream=config.STREAM_JSON,
                token_limit=token_limit,
            )
            timer.stop()

            if test_feedback.approved:
                criteria_approved = True
                break
        else:
            criteria_approved = True
            break

    phase_timer.stop()
    test_generation_time = phase_timer.elapsed()

    if criteria_approved:
        logger.info(f"\nTests approved in {test_generation_iterations} iteration(s) over {test_generation_time:.2f}s.\n")
    else:
        logger.info(f"\nMax test iterations reached ({test_generation_iterations}) over {test_generation_time:.2f}s — tests may not be fully validated.\n")

    # ── Code generation ───────────────────────────────────────────────────────

    logger.info("\nGenerating code...\n")

    code = ""
    passed = False
    code_generation_iterations = 0

    code_node.add_user(f"Requirements:\n{requirements}")

    phase_timer.start()

    for iteration in range(config.MAX_CODE_ITERATIONS):

        if code_generation_iterations == 0:

            timer.start()
            code = call_node(code_node, stream=config.STREAM_CODE)
            timer.stop()
            code_node.add_assistant(code)

        logger.info(f"\n--- Test Run Iteration {iteration + 1} ---\n")

        results = run_tests(code, tests)
        code_generation_iterations = iteration + 1

        logger.info(str(results["output"]))

        if results["passed"]:
            passed = True
            phase_timer.stop()
            code_generation_time = phase_timer.elapsed() - test_generation_time
            function_time = phase_timer.elapsed()
            logger.info(f"\nAll tests passed in {code_generation_iterations} iteration(s) over {code_generation_time:.2f}s.\n")
            break

        if iteration < config.MAX_CODE_ITERATIONS - 1:
            logger.info("\nAnalysing failures...")

            # Start node fresh
            diagnosis_node = Node(DIAGNOSIS_PROMPT)

            # If test check skipped, give the diagnosis node requirements to refer to
            prefix = f"Requirements:\n{requirements}\n\n" if not config.TESTS_FEEDBACK else ""

            diagnosis_node.add_user(
                f"{prefix}"
                f"Code:\n{code}\n\n"
                f"Test output:\n{results['output']}\n\n"
                "Populate the 'fixes' list for each failing test. Answer in JSON format."
            )
            timer.start()
            diagnosis = call_node(
                diagnosis_node,
                schema=Diagnosis,
                stream=config.STREAM_JSON,
            )
            timer.stop()
            diagnosis_node.add_assistant(str(diagnosis))

            logger.info(f"\n{diagnosis}")

            # Route each fix to a fresh, lightweight repair node for the appropriate side.
            code, tests = _dispatch_fixes(diagnosis, code, tests, requirements, logger)

    else:
        phase_timer.stop()
        code_generation_time = phase_timer.elapsed() - test_generation_time
        function_time = phase_timer.elapsed()
        logger.info(f"\nMax code iterations ({code_generation_iterations}) reached in {code_generation_time:.2f}s — code did not pass tests.\n")

    return code, tests


# ── Version record helper ─────────────────────────────────────────────────────
"""
def _record_run(
    directories,
    requirements: str,
    code_generation_iterations: int,
    test_generation_iterations: int,
    test_generation_time: float,
    code_generation_time: float,
    function_time: float,
    llm_time: float,
    passed: bool,
    logger: logging.Logger,
) -> None:
    notes = get_input("Notes for this run (press Enter to skip): ")
    record = {
        "Timestamp": datetime.now().isoformat(),
        "Script": Path(sys.argv[0]).stem,
        "Task": str(requirements)[:120],  # Truncate for readability in CSV
        "Serial": directories.serial,
        "Model": config.MODEL + config.MODEL_VARIANT,
        "Test Generation Iterations": test_generation_iterations,
        "Code Generation Iterations": code_generation_iterations,
        "Test Generation Duration": round(test_generation_time, 2),
        "Code Generation Duration": round(code_generation_time, 2),
        "Total Runtime": round(function_time, 2),
        "LLM Time": round(llm_time, 2),
        "Passed": passed,
        "Notes": notes,
    }
    with directories.version_csv.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=record.keys(), extrasaction="ignore").writerow(record)
    logger.info("Version record updated.")
"""