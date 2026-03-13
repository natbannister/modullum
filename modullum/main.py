from pathlib import Path
from modullum.core import setup_logger, create_run_directories
from modullum.modules import run_requirements

BASE_DIR = Path(__file__).parent


def main():
    directories = create_run_directories(BASE_DIR)
    log_file = directories.artefacts_dir / "run.log"
    logger = setup_logger(str(log_file))

    logger.info("=== modullum ===\n")
    run_requirements(BASE_DIR, logger)


if __name__ == "__main__":
    main()