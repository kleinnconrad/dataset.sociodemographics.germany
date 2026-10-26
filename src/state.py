"""
Persistent state of the pipeline.

The state lives in JSON files under ``<output>/state/``, which the workflow
commits together with the data:

- ``seen/<dataset>.json``: the items already written per dataset, with a
  content hash, so that unchanged items are not written again and changed
  items get a new revision. Large stores are split into shards
  (``seen/<dataset>/<shard>.json``, e.g. one per statistical series), so that a run
  rewrites only the shards it changes.
- ``collectors.json``: per collector the first run, the last run, failures
  and a cursor (e.g. the last fetched modification time).
- ``run_log.jsonl``: one line per run.

Each JSON file has a version key and one entry per line, so that git diffs
stay readable, and is written atomically.
"""
import contextlib
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

STATE_FILE_VERSION = 1
SEEN_DIR = "seen"
COLLECTORS_FILE = "collectors.json"
RUN_LOG_FILE = "run_log.jsonl"
MAX_ERROR_CHARS = 300
# Items dated more than this many days before the first run of their collector are marked as backfilled
BACKFILL_GRACE_DAYS = 7

SeenStatus = Literal["new", "changed", "unchanged"]


def atomic_write_text(path: Path, text: str) -> None:
    """Writes a text file so that readers see either the old or the new content.

    Args:
        path: Target file; its folder is created if needed.
        text: Content, written as UTF-8 with LF line endings.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def append_run_log(state_dir: Path, entry: Mapping[str, Any]) -> None:
    """Appends one line with the summary of a run to ``run_log.jsonl``.

    Args:
        state_dir: Folder of the state files.
        entry: JSON-compatible summary of the run.
    """
    path = state_dir / RUN_LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def dump_entries(collection: str, entries: Mapping[str, Mapping[str, Any]], version: int = STATE_FILE_VERSION) -> str:
    """Serializes entries as JSON with sorted keys and one entry per line.

    Args:
        collection: Name of the object that holds the entries, e.g. ``items``.
        entries: Entries by key.
        version: File format version.

    Returns:
        str: The JSON text.
    """
    lines = ["{", f'"version": {version},', f"{json.dumps(collection)}: {{"]
    body = [
        f"{json.dumps(key, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}"
        for key, value in sorted(entries.items())
    ]
    if body:
        lines.append(",\n".join(body))
    lines.extend(["}", "}"])
    return "\n".join(lines) + "\n"


def load_entries(path: Path, collection: str, version: int = STATE_FILE_VERSION) -> dict[str, dict[str, Any]]:
    """Reads entries written by ``dump_entries``.

    Args:
        path: The JSON file; a missing file means no entries.
        collection: Name of the object that holds the entries.
        version: Expected file format version.

    Returns:
        dict: Entries by key.

    Raises:
        ValueError: If the file has another version or no such collection.
    """
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != version:
        raise ValueError(f"{path} has version {data.get('version')!r}; expected {version}")
    entries = data.get(collection)
    if not isinstance(entries, dict):
        raise ValueError(f"{path} has no {collection!r} object")
    return entries


def content_hash(value: Any) -> str:
    """Returns a short hash of a JSON-compatible value.

    Args:
        value: The value; dictionaries are hashed with sorted keys.

    Returns:
        str: 16 hexadecimal digits.
    """
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def is_backfilled(item_date: Optional[date], first_run: date) -> bool:
    """Returns True if an item is older than the start of collection of its collector.

    Args:
        item_date: Date of the item (publication, release or measurement date); None counts as current.
        first_run: Date of the first run of the collector.

    Returns:
        bool: True if the item is dated more than BACKFILL_GRACE_DAYS before the first run.
    """
    return item_date is not None and item_date < first_run - timedelta(days=BACKFILL_GRACE_DAYS)


def seen_path(state_dir: Path, dataset: str, shard: Optional[str] = None) -> Path:
    """Returns the file of a seen store.

    Args:
        state_dir: Folder of the state files.
        dataset: Name of the dataset.
        shard: Name of the shard of a split store; None for an unsplit store.

    Returns:
        Path: ``seen/<dataset>.json`` or ``seen/<dataset>/<shard>.json``.
    """
    if shard is None:
        return state_dir / SEEN_DIR / f"{dataset}.json"
    return state_dir / SEEN_DIR / dataset / f"{shard}.json"


class SeenEntry(BaseModel):
    """An item that was already written to a dataset.

    Attributes:
        hash (str): Hash of the tracked content of the item.
        first_seen (date): Day of the first observation.
        last_seen (date): Day of the latest observation.
        revision (int): Revision of the latest written record.
        data (dict): Small collector-specific values, e.g. the last stored day of a series.
    """
    hash: str
    first_seen: date
    last_seen: date
    revision: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


class SeenStore:
    """The items already written to one dataset (or one shard of it), keyed by their natural key."""

    def __init__(self, path: Path, entries: dict[str, SeenEntry]) -> None:
        """Initializes the store.

        Args:
            path: The JSON file of the store.
            entries: Entries by natural key.
        """
        self._path = path
        self._entries = entries
        self._dirty = False

    @classmethod
    def load(cls, state_dir: Path, dataset: str, shard: Optional[str] = None) -> "SeenStore":
        """Reads the store of a dataset; a missing file means an empty store.

        Args:
            state_dir: Folder of the state files.
            dataset: Name of the dataset.
            shard: Name of the shard of a split store; None for an unsplit store.

        Returns:
            SeenStore: The loaded store.
        """
        path = seen_path(state_dir, dataset, shard)
        entries = {key: SeenEntry.model_validate(value) for key, value in load_entries(path, "items").items()}
        return cls(path, entries)

    def save(self) -> None:
        """Writes the store atomically if it changed since it was loaded or last saved."""
        if not self._dirty:
            return
        atomic_write_text(
            self._path,
            dump_entries("items", {key: entry.model_dump(mode="json") for key, entry in self._entries.items()}),
        )
        self._dirty = False

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def dirty(self) -> bool:
        """True if the store has changes that are not saved yet."""
        return self._dirty

    def keys(self) -> list[str]:
        """Returns the natural keys of all entries."""
        return list(self._entries)

    def get(self, key: str) -> Optional[SeenEntry]:
        """Returns the entry of an item, or None if it is new.

        Args:
            key: Natural key of the item.
        """
        return self._entries.get(key)

    def classify(self, key: str, item_hash: str) -> SeenStatus:
        """Tells whether an item is new, changed or unchanged.

        Args:
            key: Natural key of the item.
            item_hash: Hash of the tracked content.

        Returns:
            str: new, changed or unchanged.
        """
        entry = self._entries.get(key)
        if entry is None:
            return "new"
        return "unchanged" if entry.hash == item_hash else "changed"

    def mark(self, key: str, item_hash: str, today: date, data: Optional[Mapping[str, Any]] = None) -> int:
        """Records an observation of an item.

        Args:
            key: Natural key of the item.
            item_hash: Hash of the tracked content.
            today: Date of the run.
            data: Collector-specific values to keep with the entry; None keeps the previous values.

        Returns:
            int: The revision of the item after this observation: 0 for a new
            item, the previous revision plus one for a changed item, unchanged otherwise.
        """
        entry = self._entries.get(key)
        if entry is None:
            entry = SeenEntry(hash=item_hash, first_seen=today, last_seen=today, data=dict(data or {}))
        else:
            revision = entry.revision + (1 if entry.hash != item_hash else 0)
            entry = entry.model_copy(update={
                "hash": item_hash, "last_seen": today, "revision": revision,
                "data": dict(data) if data is not None else entry.data,
            })
        self._entries[key] = entry
        self._dirty = True
        return entry.revision

    def prune(self, today: date, retention_days: int) -> int:
        """Removes entries that were not observed within the retention period.

        Args:
            today: Date of the run.
            retention_days: Entries last seen longer ago are removed.

        Returns:
            int: Number of removed entries.
        """
        cutoff = today - timedelta(days=retention_days)
        stale = [key for key, entry in self._entries.items() if entry.last_seen < cutoff]
        for key in stale:
            del self._entries[key]
        if stale:
            self._dirty = True
        return len(stale)


class CollectorEntry(BaseModel):
    """Run history of one collector.

    Attributes:
        first_run (date): Day of the first run; defines which items count as backfilled.
        last_run (Optional[datetime]): Start of the latest run.
        last_success (Optional[datetime]): Start of the latest successful run.
        last_complete (Optional[datetime]): Start of the latest successful run that
            did not stop at the run deadline.
        consecutive_failures (int): Failed runs since the last success.
        last_error (Optional[str]): Error of the latest failed run.
        cursor (dict): Collector-specific progress, e.g. the last fetched modification time.
    """
    first_run: date
    last_run: Optional[datetime] = None
    last_success: Optional[datetime] = None
    last_complete: Optional[datetime] = None
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    cursor: dict[str, Any] = Field(default_factory=dict)


class CollectorStates:
    """Run history of all collectors (``state/collectors.json``)."""

    def __init__(self, path: Path, entries: dict[str, CollectorEntry]) -> None:
        """Initializes the states.

        Args:
            path: The JSON file.
            entries: Entries by collector name.
        """
        self._path = path
        self._entries = entries

    @classmethod
    def load(cls, state_dir: Path) -> "CollectorStates":
        """Reads the collector states; a missing file means no history.

        Args:
            state_dir: Folder of the state files.

        Returns:
            CollectorStates: The loaded states.
        """
        path = state_dir / COLLECTORS_FILE
        entries = {key: CollectorEntry.model_validate(value)
                   for key, value in load_entries(path, "collectors").items()}
        return cls(path, entries)

    def save(self) -> None:
        """Writes the file atomically."""
        atomic_write_text(
            self._path,
            dump_entries("collectors", {key: entry.model_dump(mode="json") for key, entry in self._entries.items()}),
        )

    def entry(self, name: str, today: date) -> CollectorEntry:
        """Returns the entry of a collector, creating it on the first run.

        Args:
            name: Name of the collector.
            today: Date of the run, used as first_run for a new collector.

        Returns:
            CollectorEntry: The entry; changes are made through ``update``.
        """
        if name not in self._entries:
            self._entries[name] = CollectorEntry(first_run=today)
        return self._entries[name]

    def update(self, name: str, **changes: Any) -> CollectorEntry:
        """Changes fields of a collector entry.

        Args:
            name: Name of the collector; the entry must exist.
            **changes: Field values to set.

        Returns:
            CollectorEntry: The updated entry.
        """
        entry = self._entries[name].model_copy(update=changes)
        self._entries[name] = entry
        return entry

    def record_success(self, name: str, started: datetime, cursor: Optional[Mapping[str, Any]] = None,
                       complete: bool = True) -> None:
        """Records a successful run of a collector.

        Args:
            name: Name of the collector.
            started: Start of the run.
            cursor: New cursor; None keeps the previous one.
            complete: False if the collector stopped at the run deadline.
        """
        changes: dict[str, Any] = {"last_run": started, "last_success": started, "consecutive_failures": 0,
                                   "last_error": None}
        if complete:
            changes["last_complete"] = started
        if cursor is not None:
            changes["cursor"] = dict(cursor)
        self.update(name, **changes)

    def record_failure(self, name: str, started: datetime, error: str) -> None:
        """Records a failed run of a collector.

        Args:
            name: Name of the collector.
            started: Start of the run.
            error: Description of the failure.
        """
        entry = self._entries[name]
        self.update(name, last_run=started, consecutive_failures=entry.consecutive_failures + 1,
                    last_error=error[:MAX_ERROR_CHARS])
