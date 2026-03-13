from pathlib import Path
import logging

# Head agent will orchestrate module execution in sequence.
# Extend this as you wire up more modules.


class HeadAgent:
    """
    Orchestrates the modullum pipeline.

    Responsible for sequencing module calls, passing outputs between them,
    and surfacing results.
    """

    def __init__(self, base_dir: Path, logger: logging.Logger):
        self.base_dir = base_dir
        self.logger = logger

    def run(self):
        raise NotImplementedError("HeadAgent.run() not yet implemented.")