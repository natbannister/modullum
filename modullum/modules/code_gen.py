import logging
import re
import sys
import subprocess
import tempfile
from pathlib import Path
from pydantic import Field

from enum import Enum
from pydantic import BaseModel
from prompt_toolkit import prompt
from prompt_toolkit.styles import Style

from modullum.core import Node, schema_to_prompt_hint, call_node, status_spinner
from modullum.core.workspace import ModuleContext
from modullum.core.pane_display import PaneDisplay, StreamDisplay
from modullum.config import settings


# ── Prompt toolkit style ──────────────────────────────────────────────────────

_style = Style.from_dict({"placeholder": "#666666"})


def get_input(placeholder: str = "Send a message") -> str:
    return prompt(">>> ", placeholder=placeholder, style=_style)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class FailedNode(str, Enum):
    code = "code"
    tests = "tests"
    missing_dependency = "missing_dependency"

    def __str__(self):
        return self.value


class RootCause(BaseModel):
    failed_node: FailedNode = Field(description="Which component has the bug: 'code', 'tests', or 'missing_dependency'")
    diagnosis: str = Field(description="Explanation of the root cause")
    fix: str = Field(description="Plain text description of the fix")
    code_snippet: str | None = Field(default=None, description="Optional illustrative code snippet for the fix")
    resolves_tests: list[str] = Field(description="List of test names that will pass once this fix is applied")


class Diagnosis(BaseModel):
    root_causes: list[RootCause] = Field(
        description="List of distinct root causes. Multiple test failures may share the same root cause."
    )

    def __str__(self):
        output = []
        for rc in self.root_causes:
            tests_str = ", ".join(rc.resolves_tests)
            block = (
                f"[{rc.failed_node}]\n"
                f"Diagnosis: {rc.diagnosis}\n"
                f"Fix: {rc.fix}\n"
                f"Resolves: {tests_str}"
            )
            if rc.code_snippet:
                block += f"\n```python\n{rc.code_snippet}\n```"
            output.append(block)
        return "\n\n".join(output)
    

class TestReview(BaseModel):
    test_name: str
    requirement_id: str
    conformance: bool
    reason: str
    amendment: str | None = None

    def __str__(self):
        return f"[Test: {self.test_name}][Requirement: {self.requirement_id}][Conformance: {self.conformance}]\n[Reason: {self.reason}]\n[Amendment: {self.amendment}]"


class TestResult:
    def __init__(self, tests: list[dict], failures: list[dict]):
        self.tests = tests
        self.failures = failures
        self.passed = all(t["status"] == "PASSED" for t in tests) if len(tests)>=1 else False

    def has_failures(self):
        return bool(self.failures)

    def failure_count(self):
        return len(self.failures)


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
    "You are reviewing unit tests against their requirements. For each test, verify:\n"
    "\n"
    "1. REQUIREMENT MAPPING:\n"
    "   - Identify which requirement(s) (REQ-XXX) this test validates\n"
    "   - If multiple tests validate the same requirement, flag as redundant unless testing different edge cases\n"
    "   - Flag if any requirement has no corresponding test\n"
    "\n"
    "2. INPUT CORRECTNESS:\n"
    "   - If a requirement specifies exact parameter values, verify the test uses those EXACT values\n"
    "   - Parse test names for claimed scenarios (e.g., 'when_X_is_zero') and verify parameter X actually equals zero in the test call\n"
    "   - Check argument positions match function signature - especially critical for functions with many parameters\n"
    "\n"
    "3. ASSERTION CORRECTNESS:\n"
    "   - Verify assertions match what the requirement mandates\n"
    "   - Check that all conditions in a requirement are tested (if requirement has multiple 'SHALL' clauses, all must be checked)\n"
    "   - Ensure tolerances match requirement specifications (e.g., 'within 1e-6' in requirement = use 1e-6 in assertion)\n"
    "\n"
    "4. TECHNICAL VALIDITY:\n"
    "   - No syntax errors\n"
    "   - No logical errors (e.g., assert always True, assert unreachable)\n"
    "   - Not vacuous (actually tests something meaningful)\n"
    "   - Proper use of pytest features (pytest.raises, pytest.approx, etc.)\n"
    "\n"
    "5. COMPLETENESS:\n"
    "   - Each requirement must have at least one test that validates it\n"
    "   - Tests with specific parameter values (like REQ-010) need a dedicated test with those exact inputs\n"
    "\n"
    "Tests are approved once all are conformant. If issues found, provide specific fixes.\n"
    f"{schema_to_prompt_hint(ManagerAction)}"
)

CODE_GENERATOR_PROMPT = (
    "Use the requirements to generate Python code only. No explanation."
    f"\nInclude a comment at the start: # Generated in Modullum with {settings.model_options.model}"
)

DIAGNOSIS_PROMPT = ("""
For each failing test:

1. **Verify test validity first:**
   - Parse the test name to understand what scenario it claims to test
   - Check if the actual function call matches that scenario
   - For tests named "when X is Y", verify parameter X actually equals Y
   - Flag if test inputs don't match test name claims

2. **Trace execution order in code:**
   - Does the function modify state after validation checks?
   - Are special case handlers before or after assertions?
   - Could overwriting variables invalidate earlier checks?

3. **Look for suspicious patterns:**
   - Multiple tests with identical inputs but different expectations
   - Hardcoded values that match failure outputs
   - Control flow that skips or bypasses calculations
                    

4. **Map failures to requirements:**
   - Which REQ-XXX does this test validate?
   - Does the code implement that requirement correctly?
   - If test matches requirement but fails, bug is in CODE
   - If test contradicts requirement, bug is in TESTS

   Requirements are authoritative. Tests that correctly validate requirements are correct by definition.
"""
f"{schema_to_prompt_hint(Diagnosis)}")

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

def clean_failure_line(line: str) -> str:
    """Strip long temp paths from pytest output."""
    # Replace any absolute path with just the filename and line
    line = re.sub(r'(/[^:\s]+)+/', '', line)
    return line


def parse_pytest_output(output: str) -> TestResult:
    """
    Parse pytest output into structured results:
      - tests: all tests with status
      - failures: failed tests with full cleaned context
      - summary: concise logging summary
    """

    print(output)

    lines = output.strip().splitlines()
    tests = []
    failures = []

    test_line_pattern = re.compile(r"::(.+?)\s+(PASSED|FAILED|ERROR)(?=\s|\[|$)")

    for line_num, line in enumerate(lines):

        # Match test result lines
        match = test_line_pattern.search(line)
        if match:
            test_name, status = match.groups()
            tests.append({"name": test_name, "status": status})

    # Handle case where no tests were parsed but output exists
    if not tests and output.strip():
        print(f"Warning: Could not parse any tests from output:\n")
        for i, line in enumerate(lines):
            print(f"{i}: {repr(line)}")

    # Parse failures (failed line + reason)
    failures_text = output.partition("FAILURES")[2] # The text following "FAILURES" if present. "" if not.
    
    # Gather each failure block in the failures section
    failure_blocks = re.split(r"_+\s+(.+?)\s+_+\n", failures_text)

    for i in range(1, len(failure_blocks), 2):
        test_name = failure_blocks[i].strip()
        block = failure_blocks[i + 1]

        lines = block.splitlines()

        failure_message_lines = []
        failure_message_reasons = []

        in_failure = False
        for line in lines:
            stripped = line.lstrip()   # remove leading whitespace
            if stripped.startswith("> "):
                in_failure = True
                cleaned_line = re.sub(r"^>\s+", "", stripped)
                failure_message_lines.append(cleaned_line)
            elif in_failure and stripped.startswith("E "):
                cleaned_line = re.sub(r"^E\s+", "", stripped)
                failure_message_reasons.append(cleaned_line)
            elif in_failure and stripped.startswith("_"):
                # _ denotes a new failed test section
                break

        failure_message = "\n".join(failure_message_lines)
        failure_reason = "\n".join(failure_message_reasons)

        failures.append({
            "test_name": test_name,
            "failed_line": failure_message,
            "reason": failure_reason,
        })

    return TestResult(tests=tests, failures=failures)


def run_tests(code: str, tests: str) -> TestResult:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Write module
        module_path = tmpdir_path / "module.py"
        module_path.write_text(code)

        # Write test file
        test_path = tmpdir_path / "test_module.py"
        modified_tests = re.sub(r'from \w+ import', 'from module import', tests)
        test_path.write_text(modified_tests)

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", str(test_path), "-v"],
                capture_output=True,
                text=True,
                cwd=tmpdir,
            )
        except FileNotFoundError:
            raise RuntimeError(f"pytest is not installed in Python interpreter: {sys.executable}")

        output = result.stdout + "\n" + result.stderr

        return parse_pytest_output(output)


def _format_fixes(root_causes: list[RootCause]) -> str:
    """Renders a list of RootCause objects into a concise prompt-ready string."""
    return "\n\n".join(
        f"[{f.failed_node}] {f.fix}"
        + (f"\n```python\n{f.code_snippet}\n```" if f.code_snippet else "")
        for f in root_causes
    )


def _apply_code_fixes(
    code: str,
    requirements: str,
    root_causes: list[RootCause],
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
        f"Apply the following fixes, do NOT change any other code:\n{_format_fixes(root_causes)}"
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
    root_causes: list[RootCause],
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
        f"Apply the following fixes ONLY:\n{_format_fixes(root_causes)}"
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
    code_fixes = [f for f in diagnosis.root_causes if f.failed_node == "code"]
    test_fixes = [f for f in diagnosis.root_causes if f.failed_node == "tests"]
    dependency_fix = [f for f in diagnosis.root_causes if f.failed_node == "missing_dependency"]

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

        with status_spinner("Generating tests...\n"):
            with StreamDisplay(autoclose=True, fallback=logger.info) as pane:
                result = call_node(test_node, 
                                stream=settings.model_options.stream_code, 
                                token_limit=token_limit,
                                model=settings.model_options.model,
                                stream_display=pane,
                                )
        test_gen_llm_total += result.llm_duration_s
        test_gen_tokens_in += result.tokens_in
        test_gen_tokens_out += result.tokens_out
        tests = result.output

        logger.info(f"\nGENERATED TESTS:\n{tests}")

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
            with status_spinner("Generating feedback for tests...\n"):
                with StreamDisplay(autoclose=True, fallback=logger.info) as pane:
                    fb_result = call_node(
                        feedback_node,
                        ManagerAction,
                        stream=settings.model_options.stream_json,
                        token_limit=token_limit,
                        stream_display=pane,
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
            
            logger.info(f"\nTESTS FEEDBACK:\n{test_feedback}")

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
            with status_spinner("Generating code...\n"):
                with StreamDisplay(autoclose=True, fallback=logger.info) as pane:
                    result = call_node(code_node, 
                                    stream=settings.model_options.stream_code,
                                    model=settings.model_options.model,
                                    stream_display=pane,
                                    )
            code_gen_llm_total += result.llm_duration_s
            code_gen_tokens_in += result.tokens_in
            code_gen_tokens_out += result.tokens_out
            code = result.output
            code_node.add_assistant(code)

        logger.info(f"\n--- Test Run Iteration {iteration + 1} ---\n")

        results = run_tests(code, tests)
        code_generation_iterations = iteration + 1

        with PaneDisplay(autoclose=False, fallback=logger.info) as pane:
            if settings.code.output_pytest:
                # Show full pytest output
                #pane.write(str(results.get("output", results.get("summary", ""))))
                pane.write(f"\n{results.passed}\n")
                pane.write(f"\n{results.tests}\n")
                pane.write(f"\n{results.failures}\n")
            else:
                # Show only the concise summary
                pane.write(str(results.get("summary", "")))

        logger.info(f"\nPassed: {results.passed}\nTests: {results.tests}\nFailures: {results.failures}")

        if results.passed:
            passed = True
            logger.info(f"\nAll tests passed in {code_generation_iterations} iteration(s).\n")
            break

        if iteration < settings.code.max_code_iterations - 1:
            logger.info("\nAnalysing failures...")

            diagnosis_node = Node(DIAGNOSIS_PROMPT)
            prefix = f"Requirements:\n{requirements}\n\n" #if not settings.code.tests_review else ""
            diagnosis_node.add_user(
                f"{prefix}"
                f"Code:\n{code}\n\n"
                f"Test failures:\n{results.failures}\n\n"
                "Analyse all failures to identify root causes. Multiple test failures may share "
                "the same root cause. If one code change would fix multiple test failures, only include "
                "that fix once. Explain which tests each fix will resolve."
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