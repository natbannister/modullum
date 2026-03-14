import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from prompt_toolkit import prompt
from prompt_toolkit.styles import Style

from modullum.core import Node, call_node, Stopwatch, create_run_directories
from modullum import config

# ── Prompt toolkit style ──────────────────────────────────────────────────────

_style = Style.from_dict({"placeholder": "#666666"})


def get_input(placeholder: str = "Send a message") -> str:
    return prompt(">>> ", placeholder=placeholder, style=_style)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class Question(BaseModel):
    question: str
    answer: str

    def __str__(self):
        return f"Q: {self.question}\nA: {self.answer}"


class QuestionsList(BaseModel):
    questions: list[Question]

    def __str__(self):
        return "\n".join(str(q) for q in self.questions)


class Requirement(BaseModel):
    id: str
    type: str
    testability: str
    status: str
    requirement: str

    def __str__(self):
        return f"[{self.id}][{self.type}][{self.testability}][{self.status}] - {self.requirement}"


class RequirementsList(BaseModel):
    requirements: list[Requirement]

    def __str__(self):
        return "\n".join(str(r) for r in self.requirements)


# ── Prompt constants ──────────────────────────────────────────────────────────

REQUIREMENTS_SET_DEFINITION = """
Complete requirements definition set:

[1] Identity & scope
[A] What is the thing being built? (function, module, service, script)
[B] What is its name?
[C] What problem does it solve?
[D] What is explicitly out of scope?

[2] Interface
[A] What are the inputs? (name, type, units, valid range)
[B] What are the outputs? (name, type, structure)
[C] How is it called? (function call, CLI, API endpoint, event)
[D] What does it depend on that it doesn't own?

[3] Functional behaviour
[A] What does it do with valid inputs — the happy path?
[B] What algorithm or method must it use, if specified?
[C] What state does it maintain, if any?

[4] Boundary & edge cases
[A] What inputs are invalid and how should they be handled?
[B] What are the numeric/logical limits of valid inputs?
[C] What happens at the boundaries of those limits?

[5] Constraints
[A] Performance requirements (speed, memory, latency)?
[B] Platform or language constraints?
[C] Dependencies it must or must not use?
"""

INTERVIEWER_PROMPT = (
    "The user has requested a task be completed based on their prompt."
    f"\nUsing the complete requirements set definition provided, generate the "
    f"{config.INTERVIEW_QUESTION_COUNT} most important questions (related to the "
    "user's task) to make implications explicit."
    "\nSort them into JSON format."
    "\nDo not generate any answers to the questions."
)

REQUIREMENTS_GENERATOR_PROMPT = (
    "The user has requested a task be completed based on their prompt."
    f"\nUsing the complete requirements set definition provided, generate a list of "
    f"requirements (STOP AFTER {config.REQUIREMENTS_CAP} REQUIREMENTS) in the form "
    "[REQ-NNN][Type][Testability][Status]-[Requirement]."
    "\nSort them into JSON format."
    "\nRequirement types: interface, functional, validation, invariant, example, constraint"
    "\nTestability types: directly_testable, structurally_testable, implicit, not_testable"
    "\nStatus (highest to lowest confidence): confirmed, assumed, inferred"
)

ASSUMPTIONS_ANALYSER_PROMPT = (
    "Given the requirements set definition provided as a reference, what assumptions "
    "about the user's task must be made to complete it?\n"
    "Answer in plain text bullet point form ONLY with no opening statement."
)


# ── Main entry point ──────────────────────────────────────────────────────────

def run(base_dir: Path, logger: logging.Logger) -> Path:
    """
    Runs the requirements generation module.

    Args:
        base_dir: Project root (used to locate/create the runs/ directory).
        logger:   Logger instance from main.py.

    Returns:
        Path to the saved requirements.txt output file.
    """
    timer = Stopwatch()
    directories = create_run_directories(base_dir)

    # ── Get initial task ──────────────────────────────────────────────────────
    if config.USER_PROMPT:
        initial_prompt = get_input()
    else:
        initial_prompt = "Create a SEIR step modelling function"
        logger.info(f"User input skipped, defaulting to: {initial_prompt}")

    # ── Build nodes ───────────────────────────────────────────────────────────
    interviewer_node = Node(INTERVIEWER_PROMPT)
    interviewer_node.add_assistant(REQUIREMENTS_SET_DEFINITION)

    generator_node = Node(REQUIREMENTS_GENERATOR_PROMPT)
    generator_node.add_assistant(REQUIREMENTS_SET_DEFINITION)

    assumptions_node = Node(ASSUMPTIONS_ANALYSER_PROMPT)
    assumptions_node.add_assistant(REQUIREMENTS_SET_DEFINITION)

    # ── Interview ─────────────────────────────────────────────────────────────
    interview_question_count = 0

    if config.INTERVIEW:
        interviewer_node.add_user(f"Task:\n{initial_prompt}")

        timer.start()
        questions_json = call_node(
            interviewer_node, QuestionsList,
            temperature=config.TEMPERATURE,
            token_limit=config.TOKEN_LIMIT,
            model=config.MODEL,
        )
        timer.stop()
        interviewer_node.add_assistant(str(questions_json))

        logger.info("\nBefore we begin, I have a few questions.\n")
        for q in questions_json.questions:
            logger.info(f"\n{q.question}")
            if not config.AUTO_SKIP:
                answer = get_input("Your answer").strip()
            q.answer = answer if answer else "No answer provided."

        interview_question_count = len(questions_json.questions)
        scope_info = f"Additional scope information:\n{questions_json}"
        generator_node.add_assistant(scope_info)
        assumptions_node.add_assistant(scope_info)

    # ── Assumptions ───────────────────────────────────────────────────────────
    assumptions_node.add_user(f"Task:\n{initial_prompt}")
    assumptions_iterations = 1
    user_satisfied = False

    while not user_satisfied:
        timer.start()
        assumptions = call_node(
            assumptions_node,
            stream=config.STREAM_USER_FACING,
            temperature=config.TEMPERATURE,
            token_limit=config.TOKEN_LIMIT,
            model=config.MODEL,
        )
        timer.stop()
        assumptions_node.add_assistant(assumptions)

        logger.info("\nSpecify changes to the assumptions, or press Enter to accept.\n")

        user_feedback = ""

        if not config.AUTO_SKIP:
            user_feedback = get_input()

        if user_feedback == "":
            user_satisfied = True
            logger.info("Proceeding to requirements generation.\n")
        else:
            assumptions_node.add_user(user_feedback)
            assumptions_iterations += 1

    # ── Requirements generation ───────────────────────────────────────────────
    generator_node.add_user(f"Task:\n{initial_prompt}")
    generator_node.add_assistant(f"Assumptions:\n{assumptions_node.last_response()}")

    requirements_iterations = 1
    user_satisfied = False

    while not user_satisfied:
        timer.start()
        requirements_json = call_node(
            generator_node, RequirementsList,
            stream=config.STREAM_USER_FACING,
            temperature=config.TEMPERATURE,
            token_limit=config.TOKEN_LIMIT,
            model=config.MODEL,
        )
        timer.stop()
        generator_node.add_assistant(str(requirements_json))

        logger.info("\nSpecify changes to the requirements, or press Enter to accept.\n")

        user_feedback = ""

        if not config.AUTO_SKIP:
            user_feedback = get_input()

        if user_feedback == "":
            user_satisfied = True
            logger.info("Requirements accepted.\n")
        else:
            # Reset to avoid context burnout on iterative edits
            generator_node = Node(REQUIREMENTS_GENERATOR_PROMPT)
            generator_node.add_assistant(f"Last requirements:\n{requirements_json}")
            generator_node.add_user(f"Incorporate changes:\n{user_feedback}")
            requirements_iterations += 1

    # ── Save output ───────────────────────────────────────────────────────────
    requirements_file = directories.outputs_dir / "requirements.txt"
    with requirements_file.open("w") as f:
        f.write(generator_node.last_response())
    logger.info(f"Requirements saved to {requirements_file}")

    # ── Version record ────────────────────────────────────────────────────────
    notes = ""
    if not config.AUTO_SKIP:
        notes = get_input("Notes for this run (press Enter to skip): ")
    record = {
        "Timestamp": datetime.now().isoformat(),
        "Script": Path(sys.argv[0]).stem,
        "Task": initial_prompt,
        "Serial": directories.serial,
        "Model": config.MODEL + config.MODEL_VARIANT,
        "Interview Questions": interview_question_count,
        "User Assumptions Iterations": assumptions_iterations,
        "User Requirements Iterations": requirements_iterations,
        "Total Processing Time": round(timer.elapsed(), 2),
        "Notes": notes,
    }
    with directories.version_csv.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=record.keys(), extrasaction="ignore").writerow(record)
    logger.info("Version record updated.")

    return requirements_json