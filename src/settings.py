"""
Runtime settings of the pipeline, read from environment variables.

Only values that may change between runs are settings. Fixed thresholds are
module constants in the modules that use them.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
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


def _env_bool(name: str, default: bool) -> bool:
    """Reads a boolean from an environment variable ("on"/"off", "true"/"false", "1"/"0").

    Args:
        name: Name of the environment variable.
        default: Value used when the variable is unset or empty.

    Returns:
        bool: The parsed value.

    Raises:
        ValueError: If the value is not one of the accepted words.
    """
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "on", "yes"):
        return True
    if raw in ("0", "false", "off", "no"):
        return False
    raise ValueError(f"Environment variable {name} must be on or off, got {raw!r}")


def _env_secret(name: str) -> Optional[str]:
    """Reads a secret and removes surrounding whitespace and quotes.

    Args:
        name: Name of the environment variable.

    Returns:
        Optional[str]: The value, or None if it is unset or empty.
    """
    raw = os.getenv(name, "").strip().strip("\"'").strip()
    return raw or None


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
        gemini_model (str): Gemini model id used for the annotation.
        gemini_api_key (Optional[str]): Gemini API key; None stores the news items
            and press releases without annotation.
        max_gemini_calls_per_run (int): Upper bound for Gemini calls in one run,
            including retries.
        max_annotations_per_run (int): Maximum number of news articles and press
            releases annotated in one run; further items are stored without labels.
        gemini_concurrency (int): Number of Gemini requests sent at the same time.
        news_lookback_hours (int): Feed and sitemap items published longer ago
            are ignored.
        fetch_pages (bool): Whether article pages are fetched for metadata and
            lead text (where robots.txt and TDM reservations permit).
        collectors (Optional[tuple[str, ...]]): Names of the collectors to run;
            None runs all that are due.
        output_dir (Path): Folder for the datasets and ``state/``.
    """
    run_deadline_minutes: int = 25
    full_check_days: int = 28
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_api_key: Optional[str] = None
    max_gemini_calls_per_run: int = 50
    max_annotations_per_run: int = 400
    gemini_concurrency: int = 4
    news_lookback_hours: int = 96
    fetch_pages: bool = True
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
            gemini_model=os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL,
            gemini_api_key=_env_secret("GEMINI_API_KEY"),
            max_gemini_calls_per_run=_env_int("MAX_GEMINI_CALLS_PER_RUN", cls.max_gemini_calls_per_run),
            max_annotations_per_run=_env_int("MAX_ANNOTATIONS_PER_RUN", cls.max_annotations_per_run),
            gemini_concurrency=_env_int("GEMINI_CONCURRENCY", cls.gemini_concurrency),
            news_lookback_hours=_env_int("NEWS_LOOKBACK_HOURS", cls.news_lookback_hours),
            fetch_pages=_env_bool("NEWS_FETCH_PAGES", cls.fetch_pages),
            collectors=collectors,
            output_dir=default_output_dir(),
        )
