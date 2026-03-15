from pathlib import Path
from modullum.core import setup_logger, create_run_directories
from modullum.agents.head import HeadAgent

BASE_DIR = Path(__file__).parent


def main():
    directories = create_run_directories(BASE_DIR)
    log_file = directories.artefacts_dir / "run.log"
    logger = setup_logger(str(log_file))

    logger.info("\n=== modullum ===\n")
    agent = HeadAgent(BASE_DIR, logger)
    agent.run()


if __name__ == "__main__":
    main()