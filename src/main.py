"""
Entry point of a pipeline run.

Usage:
    uv run python src/main.py [--dry-run | --backfill] [--collectors NAME,...] [--output-dir PATH]
"""
import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from pipeline import COLLECTOR_ORDER, build_collectors, run_pipeline
from settings import Settings

logger = logging.getLogger(__name__)


def _collector_list(value: str) -> tuple[str, ...]:
    """Parses a comma-separated list of collector names."""
    names = tuple(name.strip() for name in value.split(",") if name.strip())
    unknown = sorted(set(names) - set(COLLECTOR_ORDER))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown collectors: {', '.join(unknown)}; known: {', '.join(COLLECTOR_ORDER)}")
    return names


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    """Parses the command-line arguments.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        argparse.Namespace: The parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Runs the German sociodemographics dataset pipeline.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="fetch the series without writing data or state")
    mode.add_argument("--backfill", action="store_true",
                      help="fetch every catalog entry, also those whose dataset is unchanged, without run intervals")
    parser.add_argument("--collectors", type=_collector_list,
                        help=f"comma-separated collectors to run (default: all due of {', '.join(COLLECTOR_ORDER)})")
    parser.add_argument("--output-dir", type=Path,
                        help="folder for datasets and state (default: data/ in GitHub Actions, "
                             "~/Downloads/german_sociodemographics_dataset locally)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Runs the pipeline once.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        int: 0 if all selected collectors succeeded, 1 otherwise.
    """
    args = parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    # One line per HTTP request would bury the pipeline log; failures are logged by the collectors
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        if settings.collectors is not None:
            _collector_list(",".join(settings.collectors))
    except (ValueError, argparse.ArgumentTypeError) as e:
        logger.error("Invalid settings: %s", e)
        return 1
    if args.output_dir is not None:
        settings = replace(settings, output_dir=args.output_dir)
    if args.collectors is not None:
        settings = replace(settings, collectors=args.collectors)
    logger.info("Output folder: %s; run deadline after %d minutes.", settings.output_dir.resolve(),
                settings.run_deadline_minutes)
    summary = run_pipeline(settings, build_collectors(), dry_run=args.dry_run, backfill=args.backfill)
    for name, outcome in summary.collectors.items():
        logger.info("%s: %s %s%s", name, outcome.status, outcome.records or "",
                    f" ({outcome.error})" if outcome.error else "")
    if summary.failed:
        logger.error("Collectors with errors: %s", ", ".join(summary.failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
