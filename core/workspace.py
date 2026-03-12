from dataclasses import dataclass
from pathlib import Path
import csv


@dataclass
class RunDirectories:
    run_dir: Path
    artefacts_dir: Path
    outputs_dir: Path
    version_csv: Path
    serial: int


def create_run_directories(
    base_dir: Path,
    csv_fields: list[str] | None = None,
) -> RunDirectories:
    """
    Creates a numbered run directory under base_dir/runs/.

    Args:
        base_dir:   Root of the project (pass Path(__file__).parent from main.py).
        csv_fields: Column headers for version_record.csv. Created on first run.
    """
    runs_dir = base_dir / "runs"
    runs_dir.mkdir(exist_ok=True)

    existing = [p.name for p in runs_dir.iterdir() if p.is_dir() and p.name.startswith("run ")]
    serials = [int(p.split("run ")[1]) for p in existing if p.split("run ")[1].isdigit()]
    serial = max(serials, default=0) + 1

    run_dir = runs_dir / f"run {serial}"
    artefacts_dir = run_dir / "artefacts"
    outputs_dir = run_dir / "outputs"
    run_dir.mkdir()
    artefacts_dir.mkdir()
    outputs_dir.mkdir()

    version_csv = runs_dir / "version_record.csv"
    if not version_csv.exists():
        csv_fields = csv_fields or [
            "Timestamp", "Script", "Task", "Serial", "Model",
            "Interview Questions", "User Assumptions Iterations",
            "User Requirements Iterations", "Total Processing Time", "Notes",
        ]
        with version_csv.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=csv_fields).writeheader()

    return RunDirectories(
        run_dir=run_dir,
        artefacts_dir=artefacts_dir,
        outputs_dir=outputs_dir,
        version_csv=version_csv,
        serial=serial,
    )