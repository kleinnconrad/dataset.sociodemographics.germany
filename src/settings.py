"""
Runtime settings of the pipeline, read from environment variables.

Only values that may change between runs are settings. Fixed thresholds are
module constants in the modules that use them.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

USER_AGENT = ("GermanSociodemographicsDataset/0.1 "
              "(+https://github.com/kleinnconrad/dataset.sociodemographics.germany)")


def _env_int(name: str, default: int) -> int:
    """Reads a positive integer from an environment variable.

    Args:
        name: Name of the environment variable.
        default: Value used when the variable is unset or empty.

    Returns:
        int: The parsed value.

    Raises:
        ValueError: If the variable is set but is not an integer of at least 1.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from e
    if value < 1:
        raise ValueError(f"Environment variable {name} must be at least 1, got {value}")
    return value


def default_output_dir() -> Path:
    """Returns the folder for records and state.

    In GitHub Actions this is ``data/`` in the repository, which the workflow
    commits. Locally it is a folder in Downloads, so that test runs do not
    change the committed dataset.

    Returns:
        Path: The output folder.
    """
    if os.getenv("GITHUB_ACTIONS") == "true":
        return Path("data")
    return Path.home() / "Downloads" / "german_sociodemographics_dataset"


@dataclass(frozen=True)
class Settings:
    """Settings that can be changed per run without code changes.

    Attributes:
        run_deadline_minutes (int): Minutes after the start of the run after which
            the collectors start no new work, so that the workflow can commit
            before its timeout.
        full_check_days (int): A catalog entry whose dataset the provider reports
            as unchanged is fetched again after this many days at the latest.
        collectors (Optional[tuple[str, ...]]): Names of the collectors to run;
            None runs all that are due.
        output_dir (Path): Folder for the datasets and ``state/``.
    """
    run_deadline_minutes: int = 25
    full_check_days: int = 28
    collectors: Optional[tuple[str, ...]] = None
    output_dir: Path = Path("data")

    @property
    def state_dir(self) -> Path:
        """Folder of the state files."""
        return self.output_dir / "state"

    @classmethod
    def from_env(cls) -> "Settings":
        """Builds the settings from environment variables, falling back to the defaults.

        Returns:
            Settings: The settings for this run.
        """
        collectors_raw = os.getenv("COLLECTORS", "").strip()
        collectors = tuple(name.strip() for name in collectors_raw.split(",") if name.strip()) or None
        return cls(
            run_deadline_minutes=_env_int("RUN_DEADLINE_MINUTES", cls.run_deadline_minutes),
            full_check_days=_env_int("FULL_CHECK_DAYS", cls.full_check_days),
            collectors=collectors,
            output_dir=default_output_dir(),
        )
