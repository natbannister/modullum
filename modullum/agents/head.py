from pathlib import Path
import logging

from modullum.core.workspace import RunContext
from modullum.modules import requirements_gen, code_gen, scope_manager


class HeadAgent:
    """
    Orchestrates the modullum pipeline.

    Receives a RunContext constructed in main.py so the logger can be
    pointed at the run directory before the agent starts.
    """

    def __init__(self, ctx: RunContext, logger: logging.Logger):
        self.ctx = ctx
        self.logger = logger

    def run(self):
        ctx = self.ctx
        task = ""
        exit_reason = "incomplete"

        try:
            self.logger.info("\nLet's get started.\n")

            # ── Requirements ──────────────────────────────────────────────────
            requirements_json = requirements_gen.run(ctx.module("requirements_gen"), self.logger)
            task = requirements_json.task
            # scope_manager.run(self.logger, requirements_json)

            # ── Code generation ───────────────────────────────────────────────
            code, tests = code_gen.run(ctx.module("code_gen"), self.logger, requirements_json)

            exit_reason = "completed"

        except KeyboardInterrupt:
            exit_reason = "keyboard_interrupt"
            self.logger.info("\nRun interrupted by user.")

        except Exception as e:
            exit_reason = "error"
            error_str = f"{type(e).__name__}: {e}"
            self.logger.error(f"\nRun failed: {error_str}")
            for mod_ctx in ctx._modules.values():
                if mod_ctx.exit_reason == "incomplete":
                    mod_ctx.set_outcome(exit_reason="error", error=error_str)
            raise

        finally:
            ctx.finalise(task=task, exit_reason=exit_reason)
            self.logger.info(f"\nRun {ctx.serial} finalised — {exit_reason}.")