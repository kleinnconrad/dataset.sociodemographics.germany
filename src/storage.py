"""
Writes records to the partitioned dataset files.

Every dataset is stored as
``<output>/<dataset>/year=YYYY/month=MM/<dataset>_YYYY-MM-DD.json``, one file
per dataset and UTC day of observation. A file is a JSON list with one record
per line, so that it is compact and git diffs stay readable. Records are
merged by ``(record_id, revision)``: a repeated run on the same day replaces
its records instead of duplicating them.
"""
import json
import logging
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from state import atomic_write_text

logger = logging.getLogger(__name__)


def partition_file(output_dir: Path, dataset: str, day: date) -> Path:
    """Returns the path of the file of a dataset and day.

    Args:
        output_dir: Root folder of the datasets.
        dataset: Name of the dataset.
        day: The UTC day of observation.

    Returns:
        Path: ``<output_dir>/<dataset>/year=YYYY/month=MM/<dataset>_YYYY-MM-DD.json``.
    """
    return output_dir / dataset / f"year={day:%Y}" / f"month={day:%m}" / f"{dataset}_{day.isoformat()}.json"


def load_records(path: Path) -> list[dict[str, Any]]:
    """Reads a records file.

    Args:
        path: The file; a missing file means no records.

    Returns:
        list[dict]: The records.

    Raises:
        ValueError: If the file does not contain a JSON list.
    """
    if not path.exists():
        return []
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"{path} does not contain a list of records")
    return records


def dumps_records(records: Sequence[dict[str, Any]]) -> str:
    """Serializes records as a JSON list with one record per line.

    Args:
        records: JSON-compatible records.

    Returns:
        str: The JSON text with a trailing newline.
    """
    if not records:
        return "[]\n"
    lines = ",\n".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records)
    return "[\n" + lines + "\n]\n"


def _record_key(record: dict[str, Any]) -> tuple[str, int]:
    """Returns the merge key of a stored record."""
    return str(record.get("record_id")), int(record.get("revision") or 0)


def write_records(output_dir: Path, dataset: str, day: date, records: Sequence[BaseModel]) -> Optional[Path]:
    """Merges records into the file of a dataset and day.

    Args:
        output_dir: Root folder of the datasets.
        dataset: Name of the dataset.
        day: The UTC day of observation.
        records: New records; records with a known ``(record_id, revision)`` replace the stored ones.

    Returns:
        Optional[Path]: The written file, or None if there were no records.
    """
    if not records:
        return None
    path = partition_file(output_dir, dataset, day)
    existing = load_records(path)
    new = [record.model_dump(mode="json") for record in records]
    new_keys = {_record_key(record) for record in new}
    kept = [record for record in existing if _record_key(record) not in new_keys]
    atomic_write_text(path, dumps_records(kept + new))
    replaced = len(existing) - len(kept)
    logger.info("Stored %d %s records in %s%s.", len(new), dataset, path,
                f" ({replaced} replaced)" if replaced else "")
    return path


def iter_dataset_files(output_dir: Path, dataset: str) -> Iterator[Path]:
    """Yields the files of a dataset in chronological order.

    Args:
        output_dir: Root folder of the datasets.
        dataset: Name of the dataset.

    Yields:
        Path: Each partition file.
    """
    root = output_dir / dataset
    if root.exists():
        yield from sorted(root.glob(f"year=*/month=*/{dataset}_*.json"))


def iter_records(output_dir: Path, dataset: str) -> Iterator[dict[str, Any]]:
    """Yields all records of a dataset in chronological order of the files.

    Args:
        output_dir: Root folder of the datasets.
        dataset: Name of the dataset.

    Yields:
        dict: Each stored record.
    """
    for path in iter_dataset_files(output_dir, dataset):
        yield from load_records(path)
