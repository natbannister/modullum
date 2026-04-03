import logging
import re
import subprocess
import tempfile
from pathlib import Path
from pydantic import Field

from enum import Enum
from pydantic import BaseModel
from prompt_toolkit import prompt
from prompt_toolkit.styles import Style

from modullum.core import Node, call_node, schema_to_prompt_hint
from modullum.core.workspace import ModuleContext
from modullum.config import settings


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
    missing_dependency = "missing_dependency" # If a test requires a dependency that's not installed

    def __str__(self):
        return self.value


class DiagnosedFix(BaseModel):
    #target_test: str = Field(description="The name of the failing test")
    failed_node: FailedNode #= Field(description="'tests' or 'code'")
    #failed_node: str = Field(description="'tests' or 'code'")
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




# ── Prompt constants ──────────────────────────────────────────────────────────

TEST_GENERATOR_PROMPT = (
    "Generate pytest tests only. No explanation. Always start your output with 'import pytest'. Do not output anything other than Python code."
    "\nAlways import the function using: from module import <function_name>. Generate one test per functional requirement."
    "\nNever implement or redefine the function in the test file. The function will be provided separately."
    "\nDo not generate tests that check function signatures or parameter counts."
    f"\nInclude a comment at the start: # Generated in Modullum with {settings.model_options.model}"
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
    f"\nInclude a comment at the start: # Generated in Modullum with {settings.model_options.model}"
)

DIAGNOSIS_PROMPT = (
    "You will receive a set of requirements, the code generated from those requirements, and the results from "
    "unit tests running the code. Analyse the failures and populate the 'fixes' list — one entry per failing test."
    "\nIf pytest failed during collection (0 items collected), the error is in the tests, not the code. Fix the error in the tests file."
    "\nIf the pytest fails due to positional arguments errors, check the code function matches the pytest signature. "
    "\nIf they do not match, use the requirements to determine which is at fault, and the fix required." if not settings.code.tests_review else ""
    "\nCheck tests conform to the requirements before assuming the code is at fault." if not settings.code.tests_review else "The code function arguments must match the test signature."
    "\nOnly diagnose issues that directly cause test failures. Ignore style, formatting, and cosmetic issues."
    f"\n{schema_to_prompt_hint(Diagnosis)}"
)

# TEMPORARY REQUIREMENTS TO SPEED UP DEVELOPMENT:
TEMP_REQUIREMENTS = """
REQ-001: Function named seir_step SHALL accept S, E, I, R, N, beta, sigma, gamma, dt as floats
         S = susceptible population
         E = exposed (infected but not yet infectious)
         I = infectious population
         R = recovered population
         N = total population
         beta = transmission rate
         sigma = rate of progression from exposed to infectious (1/incubation period)
         gamma = recovery rate
         dt = timestep

REQ-002a: SHALL return exactly 4 values
REQ-002b: SHALL return tuple (new_S, new_E, new_I, new_R)

REQ-003: SHALL use forward Euler integration:
         dS = -beta * S * I / N
         dE = beta * S * I / N - sigma * E
         dI = sigma * E - gamma * I
         dR = gamma * I
         new_S = S + dS * dt
         new_E = E + dE * dt
         new_I = I + dI * dt
         new_R = R + dR * dt

REQ-004: SHALL raise ValueError if any of S, E, I, R are negative

REQ-005: SHALL raise ValueError if N <= 0

REQ-006: SHALL raise ValueError if dt <= 0

REQ-007: SHALL raise ValueError if any of beta, sigma, gamma are negative

REQ-008: new_S + new_E + new_I + new_R SHALL equal S + E + I + R within 1e-6

REQ-009: If I = 0 and E = 0, new_S SHALL equal S, new_E SHALL equal 0,
         new_I SHALL equal 0, new_R SHALL equal R
         (no transmission without exposed or infectious individuals)

REQ-010: For S=999, E=0, I=1, R=0, N=1000, beta=0.3, sigma=0.1, gamma=0.05, dt=1.0
         new_E SHALL be greater than 0
         new_I SHALL be less than I + 1e-6
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
    ctx: ModuleContext,
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
    rec = ctx.start_node(
        role="code_repairer",
        prompt=CODE_GENERATOR_PROMPT,
        model=settings.model_options.model,
        stream=settings.model_options.stream_code,
        think=False,
        temperature=settings.model_options.temperature,
    )
    result = call_node(repair_node, stream=settings.model_options.stream_code)
    rec.finish(
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        llm_duration_s=result.llm_duration_s,
        iterations=1,
        exit_reason="completed",
        output=result.output,
    )
    ctx.record_node(rec)
    repair_node.add_assistant(result.output)
    return repair_node, result.output


def _apply_test_fixes(
    tests: str,
    requirements: str,
    fixes: list[DiagnosedFix],
    ctx: ModuleContext,
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
        f"Apply the following fixes ONLY:\n{_format_fixes(fixes)}"
    )
    rec = ctx.start_node(
        role="test_repairer",
        prompt=TEST_GENERATOR_PROMPT,
        model=settings.model_options.model,
        stream=settings.model_options.stream_code,
        think=False,
        temperature=settings.model_options.temperature,
    )
    result = call_node(repair_node, stream=settings.model_options.stream_code)
    rec.finish(
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        llm_duration_s=result.llm_duration_s,
        iterations=1,
        exit_reason="completed",
        output=result.output,
    )
    ctx.record_node(rec)
    repair_node.add_assistant(result.output)
    return repair_node, result.output


def _dispatch_fixes(
    diagnosis: Diagnosis,
    code: str,
    tests: str,
    requirements: str,
    logger: logging.Logger,
    ctx: ModuleContext,
) -> tuple[str, str]:
    """
    Partitions fixes by target node, spawns a lightweight repair node for each
    affected side, and returns the (possibly updated) code and tests.

    Both sides can be repaired in the same iteration if the diagnosis targets both.
    """
    # Revert to using FailedNode in the future if the code/tests flagging doesn't work
    code_fixes = [f for f in diagnosis.fixes if f.failed_node == "code"]
    test_fixes = [f for f in diagnosis.fixes if f.failed_node == "tests"]
    dependency_fix = [f for f in diagnosis.fixes if f.failed_node == "missing_dependency"]

    if code_fixes:
        logger.info(f"\n  Applying {len(code_fixes)} code fix(es)...")
        _, code = _apply_code_fixes(code, requirements, code_fixes, ctx)

    if test_fixes:
        logger.info(f"\n  Applying {len(test_fixes)} test fix(es)...")
        _, tests = _apply_test_fixes(tests, requirements, test_fixes, ctx)

    if dependency_fix:
        logger.info(f"\n Missing dependency error: {dependency_fix}")

    if not code_fixes and not test_fixes and not dependency_fix:
        logger.info("  Diagnosis produced no actionable fixes.")

    return code, tests


# ── Main entry point ──────────────────────────────────────────────────────────

def run(ctx: ModuleContext, logger: logging.Logger, requirements: str) -> tuple[str, str]:
    """
    Runs the code generation module.

    Args:
        ctx:          ModuleContext provided by HeadAgent.
        logger:       Logger instance from main.py.
        requirements: Requirements string passed in from requirements_gen.

    Returns:
        (code, tests) strings.
    """

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

    # Developer mode to use default requirements set as provided above
    if settings.code.skip_requirements:
        requirements = TEMP_REQUIREMENTS

    test_gen_rec = ctx.start_node(
        role="test_generator",
        prompt=TEST_GENERATOR_PROMPT,
        model=settings.model_options.model,
        stream=settings.model_options.stream_code,
        think=False,
        temperature=settings.model_options.temperature,
    )
    test_gen_llm_total = 0.0
    test_gen_tokens_in = 0
    test_gen_tokens_out = 0

    for iteration in range(settings.code.max_test_iterations):

        token_limit = settings.model_options.big_token_limit + (test_generation_iterations * settings.model_options.token_limit)

        logger.info(f"\n--- Test Iteration {iteration + 1} ---\n")

        if last_tests:
            test_node.add_assistant(f"Previous tests generated:\n{last_tests}")
            test_node.add_assistant(f"Feedback on previous tests:\n{test_feedback}")

        test_node.add_user(f"Requirements:\n{requirements}")
        result = call_node(test_node, stream=settings.model_options.stream_code, token_limit=token_limit)
        test_gen_llm_total += result.llm_duration_s
        test_gen_tokens_in += result.tokens_in
        test_gen_tokens_out += result.tokens_out
        tests = result.output

        if tests:
            last_tests = tests
            logger.info("\nTests generated.")
            test_generation_iterations = iteration + 1

        if settings.code.tests_review:
            feedback_node.add_user(f"Requirements:\n{requirements}\nTests:\n{tests}")

            feedback_rec = ctx.start_node(
                role="test_feedback",
                prompt=FEEDBACK_PROMPT,
                model=settings.model_options.model,
                stream=settings.model_options.stream_json,
                think=False,
                temperature=settings.model_options.temperature,
            )
            fb_result = call_node(
                feedback_node,
                ManagerAction,
                stream=settings.model_options.stream_json,
                token_limit=token_limit,
            )
            feedback_rec.finish(
                tokens_in=fb_result.tokens_in,
                tokens_out=fb_result.tokens_out,
                llm_duration_s=fb_result.llm_duration_s,
                iterations=1,
                exit_reason="completed",
                output=str(fb_result.output),
            )
            ctx.record_node(feedback_rec)
            test_feedback = fb_result.output

            if test_feedback.approved:
                criteria_approved = True
                break
        else:
            criteria_approved = True
            break

    test_gen_exit = "approved" if criteria_approved else "cap_reached"
    test_gen_rec.finish(
        tokens_in=test_gen_tokens_in,
        tokens_out=test_gen_tokens_out,
        llm_duration_s=test_gen_llm_total,
        iterations=test_generation_iterations,
        exit_reason=test_gen_exit,
        output=tests,
    )
    ctx.record_node(test_gen_rec)

    if criteria_approved:
        logger.info(f"\nTests approved in {test_generation_iterations} iteration(s).\n")
    else:
        logger.info(f"\nMax test iterations reached ({test_generation_iterations}) — tests may not be fully validated.\n")

    # ── Code generation ───────────────────────────────────────────────────────
    logger.info("\nGenerating code...\n")

    code = ""
    passed = False
    code_generation_iterations = 0

    code_node.add_user(f"Requirements:\n{requirements}")

    code_gen_rec = ctx.start_node(
        role="code_generator",
        prompt=CODE_GENERATOR_PROMPT,
        model=settings.model_options.model,
        stream=settings.model_options.stream_code,
        think=False,
        temperature=settings.model_options.temperature,
    )
    code_gen_llm_total = 0.0
    code_gen_tokens_in = 0
    code_gen_tokens_out = 0

    for iteration in range(settings.code.max_code_iterations):

        if code_generation_iterations == 0:
            result = call_node(code_node, stream=settings.model_options.stream_code)
            code_gen_llm_total += result.llm_duration_s
            code_gen_tokens_in += result.tokens_in
            code_gen_tokens_out += result.tokens_out
            code = result.output
            code_node.add_assistant(code)

        logger.info(f"\n--- Test Run Iteration {iteration + 1} ---\n")

        results = run_tests(code, tests)
        code_generation_iterations = iteration + 1

        if settings.code.output_pytest:
            logger.info(str(results["output"]))

        if results["passed"]:
            passed = True
            logger.info(f"\nAll tests passed in {code_generation_iterations} iteration(s).\n")
            break

        if iteration < settings.code.max_code_iterations - 1:
            logger.info("\nAnalysing failures...")

            diagnosis_node = Node(DIAGNOSIS_PROMPT)
            prefix = f"Requirements:\n{requirements}\n\n" if not settings.code.tests_review else ""
            diagnosis_node.add_user(
                f"{prefix}"
                f"Code:\n{code}\n\n"
                f"Test output:\n{results['output']}\n\n"
                "Populate the 'fixes' list for each failing test. Answer in JSON format."
            )

            diag_rec = ctx.start_node(
                role="diagnosis",
                prompt=DIAGNOSIS_PROMPT,
                model=settings.model_options.model,
                stream=settings.model_options.stream_json,
                think=False,
                temperature=settings.model_options.temperature,
            )
            diag_result = call_node(diagnosis_node, schema=Diagnosis, stream=settings.model_options.stream_json)
            diag_rec.finish(
                tokens_in=diag_result.tokens_in,
                tokens_out=diag_result.tokens_out,
                llm_duration_s=diag_result.llm_duration_s,
                iterations=1,
                exit_reason="completed",
                output=str(diag_result.output),
            )
            ctx.record_node(diag_rec)
            diagnosis = diag_result.output
            diagnosis_node.add_assistant(str(diagnosis))

            logger.info(f"\n{diagnosis}")

            code, tests = _dispatch_fixes(diagnosis, code, tests, requirements, logger, ctx)

    else:
        logger.info(f"\nMax code iterations ({code_generation_iterations}) reached — code did not pass tests.\n")

    code_gen_exit = "passed" if passed else "cap_reached"
    code_gen_rec.finish(
        tokens_in=code_gen_tokens_in,
        tokens_out=code_gen_tokens_out,
        llm_duration_s=code_gen_llm_total,
        iterations=code_generation_iterations,
        exit_reason=code_gen_exit,
        output=code,
    )
    ctx.record_node(code_gen_rec)

    # ── Save outputs and flush ────────────────────────────────────────────────
    outputs_dir = ctx.module_dir.parent / "outputs"
    code_file = outputs_dir / "code.py"
    tests_file = outputs_dir / "tests.py"
    code_file.write_text(code, encoding="utf-8")
    tests_file.write_text(tests, encoding="utf-8")
    logger.info(f"Code saved to {code_file}")
    logger.info(f"Tests saved to {tests_file}")

    ctx.set_outcome(exit_reason="passed" if passed else "cap_reached")
    ctx.flush(outputs={"code": code_file, "tests": tests_file})

    return code, tests