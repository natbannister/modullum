import logging

from pydantic import BaseModel
from pydantic import Field
from prompt_toolkit import prompt
from prompt_toolkit.styles import Style

from modullum.core import Node, schema_to_prompt_hint, call_node, status_spinner
from modullum.core.workspace import ModuleContext
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
    id: str = Field(description="REQ-NNN")
    type: str = Field(description="Interface, Functional, Validation, Invariant, Example, Constraint")
    testability: str = Field(description="'Testable' or 'Implicit'")
    requirement: str

    def __str__(self):
        return f"[{self.id}][{self.type}][{self.testability}] - {self.requirement}"


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
[A] If the task is to generate a function, there MUST be a requirement to specify the function name
[B] What are the inputs? (name, type, units, valid range)
[C] What are the outputs? (name, type, structure)
[D] How is it called? (function call, CLI, API endpoint, event)
[E] What does it depend on that it doesn't own?

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
    "\nRespond with raw JSON using the model schema. No markdown. No redundant outer brackets, either [] or {}" # qwen3.5 likes to answer in JSON markdown
    "\nDo not generate any answers to the questions."
)

REQUIREMENTS_GENERATOR_PROMPT = (
    "The user has requested a task be completed based on their prompt."
    f"\nUsing the complete requirements set definition provided, generate a list of "
    f"requirements (STOP AFTER {config.REQUIREMENTS_CAP} REQUIREMENTS)."
    f"\n{schema_to_prompt_hint(RequirementsList)}"
)

ASSUMPTIONS_ANALYSER_PROMPT = (
    "Given the requirements set definition provided as a reference, what assumptions "
    "about the user's task must be made to complete it?\n"
    "Answer in plain text bullet point form ONLY with no opening statement."
)


# ── Main entry point ──────────────────────────────────────────────────────────

def run(ctx: ModuleContext, logger: logging.Logger) -> RequirementsList:
    """
    Runs the requirements generation module.

    Args:
        ctx:    ModuleContext provided by HeadAgent. Owns recording and output paths.
        logger: Logger instance from main.py.

    Returns:
        RequirementsList of accepted requirements.
    """

    # ── Get initial task ──────────────────────────────────────────────────────
    if config.USER_PROMPT:
        initial_prompt = get_input()
    else:
        initial_prompt = "Create a SEIR step modelling function"
        logger.info(f"User input skipped, defaulting to: {initial_prompt}\n")

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

        rec = ctx.start_node(
            role="interviewer",
            prompt=INTERVIEWER_PROMPT,
            model=config.MODEL,
            stream=False,
            think=False,
            temperature=config.TEMPERATURE,
        )
        with status_spinner("\nJust a moment..."):
            result = call_node(interviewer_node, QuestionsList, model=config.MODEL)
        rec.finish(
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            llm_duration_s=result.llm_duration_s,
            iterations=1,
            exit_reason="completed",
            output=str(result.output),
        )
        ctx.record_node(rec)

        questions_json = result.output
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
    if config.ASSUMPTIONS_USER_REVIEW:
        assumptions_node.add_user(f"Task:\n{initial_prompt}")
        assumptions_iterations = 1
        user_satisfied = False
        assumptions_llm_total = 0.0
        assumptions_tokens_in = 0
        assumptions_tokens_out = 0

        rec = ctx.start_node(
            role="assumptions",
            prompt=ASSUMPTIONS_ANALYSER_PROMPT,
            model=config.MODEL,
            stream=config.STREAM_USER_FACING,
            think=False,
            temperature=config.TEMPERATURE,
        )

        while not user_satisfied:
            result = call_node(
                assumptions_node,
                stream=config.STREAM_USER_FACING,
                model=config.MODEL,
            )
            assumptions_llm_total += result.llm_duration_s
            assumptions_tokens_in += result.tokens_in
            assumptions_tokens_out += result.tokens_out
            assumptions = result.output
            assumptions_node.add_assistant(assumptions)

            logger.info("\nSpecify changes to the assumptions, or press Enter to accept.\n")

            user_feedback = ""
            if not config.AUTO_SKIP:
                user_feedback = get_input()

            if user_feedback == "":
                user_satisfied = True
                logger.info("Proceeding to requirements generation.\n")
                exit_reason = "accepted"
            else:
                assumptions_node.add_user(user_feedback)
                assumptions_iterations += 1
                exit_reason = "iterated"

        rec.finish(
            tokens_in=assumptions_tokens_in,
            tokens_out=assumptions_tokens_out,
            llm_duration_s=assumptions_llm_total,
            iterations=assumptions_iterations,
            exit_reason=exit_reason,
            output=assumptions_node.last_response(),
        )
        ctx.record_node(rec)

        generator_node.add_assistant(f"Assumptions:\n{assumptions_node.last_response()}")

    generator_node.add_user(f"Task:\n{initial_prompt}")
    requirements_iterations = 1
    user_satisfied = False
    generator_llm_total = 0.0
    generator_tokens_in = 0
    generator_tokens_out = 0

    rec = ctx.start_node(
        role="generator",
        prompt=REQUIREMENTS_GENERATOR_PROMPT,
        model=config.MODEL,
        stream=config.STREAM_REQUIREMENTS_GEN,
        think=config.REQUIREMENTS_GEN_THINK,
        temperature=config.TEMPERATURE,
    )

    while not user_satisfied:
        result = call_node(
            generator_node, RequirementsList,
            think=config.REQUIREMENTS_GEN_THINK,
            stream=config.STREAM_REQUIREMENTS_GEN,
            model=config.MODEL,
        )
        generator_llm_total += result.llm_duration_s
        generator_tokens_in += result.tokens_in
        generator_tokens_out += result.tokens_out
        requirements_json = result.output
        generator_node.add_assistant(str(requirements_json))

        logger.info(f"\nRequirements: {requirements_json}\n")
        logger.info("\nSpecify changes to the requirements, or press Enter to accept.\n")

        user_feedback = ""
        if not config.AUTO_SKIP:
            user_feedback = get_input()

        if user_feedback == "":
            user_satisfied = True
            logger.info("Requirements accepted.\n")
            exit_reason = "accepted"
        else:
            # Reset to avoid context burnout on iterative edits
            generator_node = Node(REQUIREMENTS_GENERATOR_PROMPT)
            generator_node.add_assistant(f"Last requirements:\n{requirements_json}")
            generator_node.add_user(f"Incorporate changes:\n{user_feedback}")
            requirements_iterations += 1
            exit_reason = "iterated"

    rec.finish(
        tokens_in=generator_tokens_in,
        tokens_out=generator_tokens_out,
        llm_duration_s=generator_llm_total,
        iterations=requirements_iterations,
        exit_reason=exit_reason,
        output=str(requirements_json),
    )
    ctx.record_node(rec)

    # ── Save outputs and flush ────────────────────────────────────────────────
    requirements_file = ctx.module_dir / ".." / ".." / "outputs" / "requirements.txt"
    requirements_file = requirements_file.resolve()
    requirements_file.write_text(str(requirements_json), encoding="utf-8")
    logger.info(f"Requirements saved to {requirements_file}")

    ctx.set_outcome(exit_reason="completed")
    ctx.flush(outputs={"requirements": requirements_file})

    return requirements_json