from pathlib import Path
import logging

from modullum.modules import requirements_gen, code_gen

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
        self.logger.info("HeadAgent: starting requirements generation...\n")
        requirements_json = requirements_gen.run(self.base_dir, self.logger)
        #self.logger.info(f"HeadAgent: requirements written to {requirements_file}")
        #return requirements_file
        code_gen.run(self.base_dir, self.logger, requirements_json)

