import logging
from pathlib import Path

from modullum.core import Node, call_node, Stopwatch

# ── Prompt constants ──────────────────────────────────────────────────────────

SCOPE_PROMPT = "Do not generate any code. Respond with an integer. How many functions are required to be written to satisfy the requrements below?"

# ── Main entry point ──────────────────────────────────────────────────────────

def run(logger: logging.Logger, requirements):
    """
    Runs the scope manager module.

    WORK IN PROGRESS

    """
    timer = Stopwatch()

    # ── Build nodes ───────────────────────────────────────────────────────────
    scope_node = Node(SCOPE_PROMPT)
    scope_node.add_assistant(str(requirements))

    # ── Determine scope ───────────────────────────────────────────────────────
    timer.start()
    scope = call_node(scope_node)
    timer.stop()

    # ── Output ---------───────────────────────────────────────────────────────
    logger.info(f"\nNumber of functions required: {scope}\n")

    return scope, timer.elapsed