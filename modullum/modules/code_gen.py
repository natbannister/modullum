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
    code = "code"
    tests = "tests"

    """
    def handler(self):
        return {
            FailedNode.code: handle_code_failure,
            FailedNode.tests: handle_test_failure,
        }[self]
    """
    
    def __str__(self):
        return self.value

""" Original diagnosis node
class Diagnosis(BaseModel):
    failed_node: FailedNode = Field(description="'tests' or 'code'")

    diagnosis: str
    fix: str = Field(description="Plain text description of the solution")

    def __str__(self):
        return f"Failed node: {self.failed_node}\nDiagnosis: {self.diagnosis}\nFix: {self.fix}"
"""

class DiagnosedFix(BaseModel):
    target_test: str = Field(description="The name of the failing test")
    failed_node: FailedNode = Field(description="'tests' or 'code'")
    diagnosis: str
    fix: str = Field(description="Plain text description of the fix")
    code_snippet: str | None = None

class Diagnosis(BaseModel):
    fixes: list[DiagnosedFix]

    def __str__(self):
        return "\n\n".join(
            f"[{f.failed_node}][{f.target_test}]\n{f.diagnosis}\nFix: {f.fix}"
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
    #"\nIf the 'previous tests generated' section of the prompt contains pytest tests, use the feedback to amend the tests."
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
#    "\nAny code provided in the prompt should be modified to accommodate the feedback provided in the prompt."
)

DIAGNOSIS_PROMPT = (
    "You will receive a set of requirements, the code generated from those requirements, and the results from "
    "unit tests running the code."
    f"\n{schema_to_prompt_hint(Diagnosis)}"
#    "\nYour output will be used to improve the code to pass the tests."
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
    requirements=TEMP_REQUIREMENTS

    for iteration in range(config.MAX_TEST_ITERATIONS):

        # Increase token limit with each iteration so it doesn't truncate on subsequent cycles
        token_limit = config.TOKEN_LIMIT + (test_generation_iterations * config.TOKEN_LIMIT)

        logger.info(f"--- Test Iteration {iteration + 1} ---")

        if last_tests:
            test_node.add_assistant(f"Previous tests generated:\n{last_tests}")
            test_node.add_assistant(f"Feedback on previous tests:\n{test_feedback}")

        test_node.add_user(f"Requirements:\n{requirements}")
        timer.start()
        #with status_spinner("Generating tests..."):
        tests = call_node(
            test_node,
            stream=config.STREAM_CODE,
            token_limit=token_limit,
        )
        timer.stop()

        if tests:
            #tests_file = directories.artefacts_dir / f"tests_{iteration + 1}.py"
            #tests_file.write_text(tests)
            last_tests = tests
            logger.info("\nTests generated.")
            test_generation_iterations = iteration + 1
            #logger.info(f" Saved to {tests_file.name}.")

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

            """"
            if test_feedback:
                feedback_file = directories.artefacts_dir / f"test_feedback_{iteration + 1}.txt"
                feedback_file.write_text(test_feedback)
                logger.info(f"Test feedback saved to {feedback_file.name}.")
            """
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
    #logger.info("Generating code...\n")

    code = ""
    diagnosis = ""
    passed = False
    code_generation_iterations = 0
    tests_at_fault = False # TODO: Fix this lazy implementation later (make proper routing)

    code_node.add_user(f"Requirements:\n{requirements}") # Must be at least one user entry for call_node()

    phase_timer.start()

    for iteration in range(config.MAX_CODE_ITERATIONS):
        logger.info(f"--- Code Iteration {iteration + 1} ---")
        if not tests_at_fault:

            timer.start()
            #with status_spinner("Generating code..."):
            code = call_node(
                code_node,
                stream=config.STREAM_CODE,
            )
            timer.stop()

            logger.info("\nCode generated. Running tests...")

        results = run_tests(code, tests)
        code_generation_iterations = iteration + 1

        if results["passed"]:
            passed = True
            phase_timer.stop()
            code_generation_time = phase_timer.elapsed() - test_generation_time
            function_time = phase_timer.elapsed()

            logger.info(f"\nAll tests passed in {code_generation_iterations} iteration(s) over {code_generation_time:.2f}s.\n")

            """
            output_code = directories.outputs_dir / "output_code.py"
            output_tests = directories.outputs_dir / "output_tests.py"
            output_code.write_text(code)
            output_tests.write_text(tests)
            logger.info(f"Code saved to {output_code.name} and {output_tests.name}.")

            _record_run(directories, requirements, code_generation_iterations,
                        test_generation_iterations, test_generation_time,
                        code_generation_time, function_time, timer.elapsed(), passed, logger)

            return output_code
            """
        """
        if code:
            code_file = directories.artefacts_dir / f"code_{iteration + 1}.py"
            code_file.write_text(code)
            logger.info(f"Tests failed. Code saved to {code_file.name}.")
        """

        if iteration < config.MAX_CODE_ITERATIONS - 1:
            logger.info("\nAnalysing test failure...")

            diagnosis_node.add_user(
                f"Requirements:\n{requirements}\n\n"
                f"Code:\n{code}\n\n"
                f"Failures:\n{results['output']}\n\n"
                "\nDiagnose the failure and suggest the fix" \
                "\nAnswer in JSON format. "
                #"\nIf the function code was to blame, specify 'code' in the failed node field."
            )
            timer.start()
            diagnosis = call_node(
                diagnosis_node,
                schema=Diagnosis,
                stream=config.STREAM_JSON,
            )
            timer.stop()

            logger.info(str(diagnosis.fixes))

            if diagnosis.failed_node == "tests": # TODO: Proper node flow control. This just spawns a fresh test node.
                tests_at_fault = True
                test_node = Node(TEST_GENERATOR_PROMPT)
                test_node.add_assistant(f"Previously generated tests: {tests}")
                test_node.add_user(f"Amend tests based on the following feedback: {diagnosis.fixes}")
                tests = call_node(test_node, stream=config.STREAM_CODE)
            elif diagnosis.failed_node == "code":
                tests_at_fault = False
                code_node.add_assistant(f"\n\nPrevious code submitted:\n{code}")
                code_node.add_user(f"Amend code based on the following feedback: {diagnosis.fixes}")
            else:
                logger.info("\nFailure not attributed to either tests or code.")

            """
            if diagnosis:
                diagnosis_file = directories.artefacts_dir / f"diagnosis_{iteration + 1}.txt"
                diagnosis_file.write_text(str(diagnosis))
                logger.info(f"Diagnosis saved to {diagnosis_file.name}.")
            """

    phase_timer.stop()
    code_generation_time = phase_timer.elapsed() - test_generation_time
    function_time = phase_timer.elapsed()

    logger.info(f"\nMax code iterations ({code_generation_iterations}) reached in {code_generation_time:.2f}s — code did not pass tests.\n")
    """
    _record_run(directories, requirements, code_generation_iterations,
                test_generation_iterations, test_generation_time,
                code_generation_time, function_time, timer.elapsed(), passed, logger)

    return directories.outputs_dir / "output_code.py"
    """
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