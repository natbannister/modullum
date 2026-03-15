from pathlib import Path
import logging

from modullum.core import create_run_directories
from modullum.modules import requirements_gen, code_gen, scope_manager

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

        directories = create_run_directories(self.base_dir)

        self.logger.info("HeadAgent: starting requirements generation...\n")
        # ========== UNBLANK ONCE WORKING
        #requirements_json = requirements_gen.run(self.base_dir, self.logger)
        #scope_manager.run(self.logger, requirements_json)

        #self.logger.info(f"HeadAgent: requirements written to {requirements_file}")
        #return requirements_file

        # =========== While blanking out requirments module
        requirements_json = ""

        code, tests = code_gen.run(self.base_dir, self.logger, requirements_json)
        self.logger.info(str(code))
        self.logger.info(str(tests))


        log_dir = directories.artefacts_dir
        log_dir.mkdir(parents=True, exist_ok=True)  # Directory already exists but safeguard doesn't hurt
        log_file = log_dir / "run.log"

