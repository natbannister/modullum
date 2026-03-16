from pathlib import Path
from modullum.core import setup_logger
from modullum.core.workspace import RunContext
from modullum.agents.head import HeadAgent

BASE_DIR = Path(__file__).parent


def main():
    ctx = RunContext(BASE_DIR)
    log_file = ctx.run_dir / "run.log"
    logger = setup_logger(str(log_file))

    logger.info("\n=== modullum ===\n")
    agent = HeadAgent(ctx, logger)
    agent.run()


if __name__ == "__main__":
    main()