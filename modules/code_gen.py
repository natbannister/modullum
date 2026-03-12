import ollama
import subprocess
import tempfile
import re
import time
import logging
import csv
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel

# =========================
# ======== SETTINGS =======
# =========================

# Model used for all nodes
model = "qwen2.5-coder"
model_variant = " (7B)"

# Output requirements assessment? Y/N
req_check = False

# =========================
# ====== INPUT PROMPT =====
# =========================

task = "SEIR epidemiological model"

"""
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

REQ-002: SHALL return tuple (new_S, new_E, new_I, new_R)

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

requirements = """
[REQ-001][functional][directly_testable][confirmed] - The SEIR step modelling function should accept parameters for the current state of the population (S, E, I, R), the time step (dt), and the model parameters (beta, sigma, gamma).
[REQ-002][functional][directly_testable][confirmed] - The SEIR step modelling function should update the state of the population based on the discrete Euler-step formulations of the SEIR model equations.
[REQ-003][functional][directly_testable][confirmed] - The SEIR step modelling function should return the updated state of the population as a tuple (S, E, I, R).
[REQ-004][validation][directly_testable][confirmed] - The SEIR step modelling function should handle edge cases, such as dt too large causing instability, or compartment values exceeding N.
[REQ-006][example][directly_testable][confirmed] - The SEIR step modelling function should be able to handle a population of 1000 individuals with an initial state of S=999, E=1, I=0, R=0, and a time step of 1 day.
[REQ-007][example][directly_testable][confirmed] - The SEIR step modelling function should produce a valid output for the given example input, where valid means all compartments are non-negative and sum equals N.
[REQ-008][constraint][directly_testable][confirmed] - The total population N must be conserved, i.e., S + E + I + R should remain constant after each step.
[REQ-010][example][directly_testable][confirmed] - The SEIR step modelling function should produce a valid time series output for a given set of parameters and initial conditions, where valid means all compartments are non-negative, sum equals N, and ideally a known expected output for at least one step given fixed parameters.
"""
# =========================
# ==== REQS PROCESSING ====
# =========================

function_gen_count = ollama.chat(
    model=model,
    messages=[
        {"role": "system", "content": "Do not generate any code. Respond with an integer. How many functions are required to be written to satisfy the requrements below?"},
        {"role": "user", "content": f"Requirements:\n{requirements}"}
    ]
)["message"]["content"]

print(f"\n{function_gen_count} function(s) needs to be generated to satisfy the requirements.")

# Requirement definition structure (types: input, output, functional, non-functional)
# Alternatively: interface, functional, boundary, constraint, validation or invariant
class Requirements(BaseModel):
  serial: int
  type: str
  req: str

class RequirementsList(BaseModel):
  reqs: list[Requirements]

# =========================
# ========= ADMIN =========
# =========================

# Remove code fences from output
def strip_code_fences(text):
    match = re.search(r'```(?:python)?\n(.*?)```', text, re.DOTALL)
    return match.group(1) if match else text

# Script outputs directories structure
@dataclass
class RunDirectories:
    run_dir: Path
    artefacts_dir: Path
    outputs_dir: Path
    version_csv: Path
    serial: int

# Function output structure
@dataclass
class ModuleOutput:
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

# Create directories for all outputs
def create_run_directories(base_dir: Path = Path("runs"), script_name: str = None, csv_fields=None) -> RunDirectories:
    script_path = Path(sys.argv[0]).resolve()
    script_dir = script_path.parent  # run folders live here

    # Determine next serial
    existing = [p.name for p in script_dir.iterdir() if p.is_dir() and p.name.startswith("run ")]
    serials = [int(p.split("run ")[1]) for p in existing if p.split("run ")[1].isdigit()]
    serial = max(serials, default=0) + 1

    # Run folder and subfolders
    run_dir = script_dir / f"run {serial}"
    run_dir.mkdir()
    artefacts_dir = run_dir / "artefacts"
    outputs_dir = run_dir / "outputs"
    artefacts_dir.mkdir()
    outputs_dir.mkdir()

    # Version record CSV in the parent folder
    version_csv = script_dir.parent / "version_record.csv"
    if not version_csv.exists():
        if csv_fields is None:
            # Core fields
            csv_fields = ["Timestamp",
                          "Script",
                          "Task",
                          "Serial", 
                          "Model",
                          "Test Generation Iterations", 
                          "Code Generation Iterations",
                          "Test Generation Duration",
                          "Code Generation Duration",
                          "Total Runtime",
                          "Passed",
                          "Notes"]

        with version_csv.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_fields)
            writer.writeheader()

    return RunDirectories(run_dir=run_dir,
                   artefacts_dir=artefacts_dir,
                   outputs_dir=outputs_dir,
                   serial=serial,
                   version_csv=version_csv)

directories = create_run_directories()

# =========================
# ===== LOGGING SETUP =====
# =========================

# Use the artefacts folder from the dataclass
log_dir = directories.artefacts_dir
log_dir.mkdir(parents=True, exist_ok=True)  # Directory already exists but safeguard doesn't hurt
log_file = log_dir / "run.log"

# Logger setup
logger = logging.getLogger("run_logger") # Generates a specific instance of logger in case this becomes multi-script
logger.setLevel(logging.DEBUG)  # '.DEBUG' captures all levels in case it's needed

# Terminal handler (no timestamps)
console_handler = logging.StreamHandler()
console_formatter = logging.Formatter('%(message)s')
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# File handler (with timestamp)
file_handler = logging.FileHandler(log_file)
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# =========================
# ===== TEST FUNCTION =====
# =========================

# Unit testing function
def run_tests(code, tests):
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(f"{tmpdir}/module.py", "w") as f:
            f.write(code)
        with open(f"{tmpdir}/test_module.py", "w") as f:
            f.write(re.sub(r'from \w+ import', 'from module import', tests))
        #with open(f"{tmpdir}/conftest.py", "w") as f:
        #    f.write("")  # Empty conftest makes pytest treat tmpdir as a package root
        
        result = subprocess.run(
            ["python3", "-m", "pytest", f"{tmpdir}/test_module.py", "-v"],
            capture_output=True, text=True,
            cwd=tmpdir  # Run pytest from within the temp directory
        )
        
        return {
            "passed": result.returncode == 0,
            "output": result.stdout + result.stderr
        }
    
# =========================
# ====== NODES SETUP ======
# =========================

TEST_GENERATOR_PROMPT = """
Generate pytest tests only. No explanation. Always start your output with 'import pytest'.
Always import the function using: from module import <function_name>. Generate one test per functional requirement.
Never implement or redefine the function in the test file. The function will be provided separately.
Do not generate tests that check function signatures or parameter counts. Always import using: from module import ...
If the 'previous tests generated' section of the prompt contains pytest tests, use the feedback to amend the code.
"""

test_node = Node(TEST_GENERATOR_PROMPT)

test_node.add_assistant(f"\n\nRequirements:\n{requirements}")

last_tests=""
test_feedback=""

FEEDBACK_PROMPT = """
Use the requirements list to determine whether each test provided (1) correctly identifies whether the requirement(s) will be satisfied by a separate function designed to meet the requirement, (2) does not contain any syntactical or logical errors and (3) is not vacuous.
For any tests that do not meet the above criteria, specify the test, corresponding requirement, and non-conformance reason. If all tests meet the criteria, output ONLY 'True'.
"""

feedback_node = Node(FEEDBACK_PROMPT)

# =========================
# ======== MODULE =========
# =========================

start = time.time()

# MAIN SUBMODULE - GENERATE UNIT TESTS, THEN GENERATE CODE, THEN RUN TESTS
def run_generation_loop(requirements, max_code_iterations=5, max_test_iterations=3):

    passed = False

    function_start = time.time()

    logger.info("\nGenerating unit tests...")
    # Start task timer
    test_generation_start = time.time()
    
    for iteration in range(max_test_iterations):

        # Success flag
        criteria_approved = False

        logger.info(f"\n--- Test Iteration {iteration + 1} ---")
        # Generate tests from requirements

        # === TEST GENERATOR NODE ===

        if last_tests:
            test_node.add_assistant(f"\n\nPrevious tests generated:\n{last_tests}")
            test_node.add_assistant(f"\n\nFeedback on previous tests:\n{test_feedback}")

        tests = strip_code_fences(call_node(test_node))
        #test_node.add_assistant(str(tests))

        if tests:
            tests_file = directories.artefacts_dir / f"tests_{iteration+1}.py"
            with tests_file.open("w") as f:
                f.write(tests)
            last_tests=tests
            logger.info(f"\nTests generated. Saved to {tests_file.name}.")

        # === VALIDATION MANAGER NODE ===

        feedback_node.add_assistant(f"Requirements: {requirements}\nTests: {tests}")
        test_feedback = call_node(feedback_node)

        #################### I STOPPED UPDATING HERE ################

        if test_feedback == "True":
            criteria_approved = True
            test_generation_iterations = iteration + 1
            break

        if test_feedback:
            feedback_file = directories.artefacts_dir / f"test_feedback_{iteration+1}.txt"
            with feedback_file.open("w") as f:
                f.write(test_feedback)

            logger.info(f"\nTest feedback saved to {feedback_file.name}")

    test_generation_time = time.time() - test_generation_start
    if criteria_approved:
        logger.info(f"\nTests deemed fully functional in {iteration+1} iteration(s) over {test_generation_time:.2f} seconds.")
    else:
        logger.info(f"\nTests generated in {iteration+1} iteration(s) over {test_generation_time:.2f} seconds, but may not have satisfied the validation supervisor.")

    test_generation_iterations = iteration + 1
        
    diagnosis = ""
    code = ""
    
    logger.info("\nGenerating code tests...")
    # Start task timer
    code_generation_start = time.time()

    for iteration in range(max_code_iterations):
        logger.info(f"\n--- Code Iteration {iteration + 1} ---")
        
        # Generate code
        prompt = f"Requirements:\n{requirements}"
        if code:
            prompt += f"\n\nPrevious code submitted:\n{code}"
        if diagnosis:
            prompt += f"\n\nGuidance:\n{diagnosis}"
        
        # === CODE GENERATOR NODE ===
        code = strip_code_fences(ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": "Use the requirements to generate Python code only. No explanation. Any code provided in the prompt should be modified to accommodate the feedback provided in the prompt."},
                {"role": "user", "content": prompt},
            ]
        )["message"]["content"])

        logger.info(f"\nCode generated. Testing...")
        
        # Run tests
        results = run_tests(code, tests)
        
        if results["passed"]:

            # Stop the clock
            function_time = time.time() - function_start

            passed = True

            logger.info("\nAll tests passed.")

            code_generation_iterations = iteration + 1
            code_generation_time = time.time() - code_generation_start

            logger.info(f"\nValidated code generated and tested in {iteration+1} iteration(s) over {code_generation_time:.2f} seconds.")


            return ModuleOutput(
                code=code,
                tests=tests,
                max_test_iterations=max_test_iterations,
                max_code_iterations=max_code_iterations,
                test_generation_iterations=test_generation_iterations,
                code_generation_iterations=code_generation_iterations,
                test_generation_time=test_generation_time,
                code_generation_time=code_generation_time,
                function_time=function_time,
                passed=passed
            )
        
        # Only save code in artefacts folder if it's not successful, otherwise it gets saved twice if successful
        if code:
            code_file = directories.artefacts_dir / f"code_{iteration+1}.py"
            with code_file.open("w") as f:
                f.write(code)

            logger.info(f"\nTests failed. Code saved to {code_file.name}.")
        
        # Diagnose failure

        if iteration < max_code_iterations - 1: # No point running feedback loop if it doesn't go back into code generation

            logger.info("\nAnalysing the test failure...")

            # === CODE DIAGNOSIS NODE ===
            diagnosis = strip_code_fences(ollama.chat(
                model=model,
                messages=[
                    {"role": "system", "content": "You will receive a set of requirements, the code generated from those requirements, and the results from unit tests running the code. "
                    "Respond in plain English only. Do not include any code in your output. Your output will be used to improve the code to pass the tests."},
                    {"role": "user", "content": f"""
                    Requirements: {requirements}
                    Code: {code}
                    Failures: {results['output']}
                    In bullet points (*), for each failure, explain how the test-failing parts of code should be written to ensure the test passes.
                    """}
                ]
            )["message"]["content"])

            logger.info(f"\nDiagnosis: {diagnosis[:1000]}...")

            if diagnosis:
                diagnosis_file = directories.artefacts_dir / f"diagnosis_{iteration+1}.txt"
                with diagnosis_file.open("w") as f:
                    f.write(diagnosis)

            logger.info(f"\nFeedback saved to {diagnosis_file.name}.")

    code_generation_iterations = iteration + 1
    code_generation_time = time.time() - code_generation_start
    function_time = time.time() - function_start

    logger.info(f"\nMax code generations iterations ({iteration+1}) reached in {code_generation_time:.2f} seconds — code did not pass tests.")

    return ModuleOutput(
        code=code,
        tests=tests,
        max_test_iterations=max_test_iterations,
        max_code_iterations=max_code_iterations,
        test_generation_iterations=test_generation_iterations,
        code_generation_iterations=code_generation_iterations,
        test_generation_time=test_generation_time,
        code_generation_time=code_generation_time,
        function_time=function_time,
        passed=passed,
    )

# Run it
outputs = run_generation_loop(requirements)

# Save the output if successful
if outputs.code:
    code_file = directories.outputs_dir / "output_code.py"
    tests_file = directories.outputs_dir / "output_tests.py"

    with code_file.open("w") as f:
        f.write(outputs.code)
    with tests_file.open("w") as f:
        f.write(outputs.tests)

    logger.info(
        f"\nCode saved to {code_file.name} and {tests_file.name}"
    )

# Report outcome
total_time = time.time() - start
if req_check:
    logger.info(f"\nCompleted in {outputs.code_generation_iterations} code-generation iteration(s) over {outputs.function_time:.2f} seconds ({total_time:.2f} seconds including requirements check).")
else:
    logger.info(f"\nCompleted in {outputs.code_generation_iterations} code-generation iteration(s) over {total_time:.2f} seconds.")

# Save results
timestamp = datetime.now().isoformat()

notes = input("Type any notes for this run and press [ENTER].")

record = {
    "Timestamp": timestamp,
    "Script": Path(sys.argv[0]).stem, # Script name minus extension
    "Task": task,
    "Serial": directories.serial,
    "Model": model + model_variant,
    "Test Generation Iterations": outputs.test_generation_iterations,
    "Code Generation Iterations": outputs.code_generation_iterations,
    "Test Generation Duration": round(outputs.test_generation_time, 2), # Rounding to 2 decimal places keeps the .csv neat
    "Code Generation Duration":round(outputs.code_generation_time, 2),
    "Total Runtime":round(outputs.function_time, 2),
    "Passed": outputs.passed,
    "Notes": notes
}

with directories.version_csv.open("a", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=record.keys(), extrasaction='ignore')
    writer.writerow(record)

logger.info(f"\nUpdated version record with results.")