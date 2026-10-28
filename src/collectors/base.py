"""
Common interface of the collectors and the context of a run.

A collector reads one source family and emits records of one or more
datasets. It receives a ``RunContext`` with the settings, the HTTP client,
the seen stores and a clock, and its own ``CollectorEntry`` with the first
run date and the cursor of the previous run. Records are written through
``RunContext.emit``, which also saves the seen stores of the dataset, so that
an interrupted run keeps what it has written.

Three kinds of datasets share these helpers:
- vintage datasets (``sociodemographic_series``) store a statistical series
  in full when it appears and afterwards, with the next revision, only the
  periods that were added, revised or removed at the provider;
- change datasets (``series_catalog``, ``regions``, ``press_catalog``) store
  an item when it appears and again, with the next revision, when a tracked
  field changes;
- event datasets (news items, press releases, annotations) store an item
  once when it is observed.
"""
import logging
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, ClassVar, Optional, TypeVar

from schema import RecordBase
from settings import Settings
from state import CollectorEntry, SeenStore
from storage import write_records
from web import WebClient

logger = logging.getLogger(__name__)

T = TypeVar("T")


def utc_now() -> datetime:
    """Returns the current UTC time without microseconds."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def batches(values: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Yields consecutive slices of at most ``size`` values.

    Args:
        values: The values.
        size: Maximum length of a slice.

    Yields:
        Sequence: Each slice.
    """
    for start in range(0, len(values), size):
        yield values[start:start + size]


class CollectorSkipped(Exception):
    """A collector cannot run because a prerequisite is missing, e.g. a configuration file."""


@dataclass
class CollectorResult:
    """Outcome of one collector run.

    Attributes:
        cursor (Optional[dict]): New cursor to store; None keeps the previous one.
        counters (dict): Counters for the run log.
        error (Optional[str]): Set if the collector wrote its data but a part of
            the work failed; the run then exits with code 1.
        complete (bool): False if the collector stopped at the run deadline or at
            a quota; the remaining work follows in the next run.
    """
    cursor: Optional[dict[str, Any]] = None
    counters: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    complete: bool = True


class RunContext:
    """Shared resources of one pipeline run.

    Attributes:
        settings (Settings): The run settings.
        web (WebClient): The HTTP client.
        started (datetime): Start of the run (UTC).
        dry_run (bool): If True, nothing is written.
        backfill (bool): If True, the run imports the history without the per-run caps.
        deadline (datetime): Time after which the collectors start no new work.
        written (Counter): Number of records emitted per dataset.
    """

    def __init__(
        self,
        settings: Settings,
        web: WebClient,
        started: datetime,
        dry_run: bool = False,
        clock: Callable[[], datetime] = utc_now,
        backfill: bool = False,
    ) -> None:
        """Initializes the context.

        Args:
            settings: The run settings.
            web: The HTTP client.
            started: Start of the run (UTC).
            dry_run: If True, records are counted but not written, and no state is saved.
            clock: Returns the current UTC time; replaceable in tests.
            backfill: If True, the collectors import the history without the per-run caps.
        """
        self.settings = settings
        self.web = web
        self.started = started
        self.dry_run = dry_run
        self.backfill = backfill
        self.deadline = started + timedelta(minutes=settings.run_deadline_minutes)
        self._clock = clock
        self._seen: dict[tuple[str, Optional[str]], SeenStore] = {}
        self.written: Counter[str] = Counter()

    @property
    def today(self) -> date:
        """UTC day of the run; the partition of all records of the run."""
        return self.started.date()

    def now(self) -> datetime:
        """Current UTC time, used as observed_at of new records."""
        return self._clock()

    def past_deadline(self) -> bool:
        """Tells whether the run deadline has passed.

        Returns:
            bool: True if the collectors should start no new work.
        """
        return self.now() >= self.deadline

    def seen(self, dataset: str, shard: Optional[str] = None) -> SeenStore:
        """Returns the seen store of a dataset or of one of its shards, loading it on first use.

        Args:
            dataset: Name of the dataset.
            shard: Name of the shard of a split store; None for an unsplit store.

        Returns:
            SeenStore: The store.
        """
        key = (dataset, shard)
        if key not in self._seen:
            self._seen[key] = SeenStore.load(self.settings.state_dir, dataset, shard)
        return self._seen[key]

    def envelope(
        self,
        dataset: str,
        record_id: str,
        *,
        revision: int,
        source_id: str,
        source_url: Optional[str],
        license: str,
        backfilled: bool,
    ) -> dict[str, Any]:
        """Returns the common fields of a new record.

        Args:
            dataset: Name of the dataset.
            record_id: Stable id of the item.
            revision: Revision from the seen store.
            source_id: Id of the source.
            source_url: URL of the item or API resource.
            license: License or terms of the source.
            backfilled: Whether the item predates the start of collection.

        Returns:
            dict: Keyword arguments for a ``RecordBase`` subclass.
        """
        return {
            "record_id": record_id, "revision": revision, "dataset": dataset, "observed_at": self.now(),
            "source_id": source_id, "source_url": source_url, "license": license, "backfilled": backfilled,
        }

    def emit(self, dataset: str, records: Sequence[RecordBase]) -> None:
        """Writes records to the partition of the run day and saves the seen stores of the dataset.

        Args:
            dataset: Name of the dataset.
            records: The records; all must belong to ``dataset``.

        Raises:
            ValueError: If a record belongs to another dataset.
        """
        if not records:
            return
        for record in records:
            if record.dataset != dataset:
                raise ValueError(f"Record {record.record_id} belongs to {record.dataset}, not {dataset}")
        self.written[dataset] += len(records)
        if self.dry_run:
            return
        write_records(self.settings.output_dir, dataset, self.today, records)
        for (store_dataset, _shard), store in self._seen.items():
            if store_dataset == dataset:
                store.save()

    def save_seen(self) -> None:
        """Saves all loaded seen stores that changed (not in dry-run mode)."""
        if self.dry_run:
            return
        for store in self._seen.values():
            store.save()

    def discard_unsaved(self) -> int:
        """Drops the loaded seen stores that have unsaved changes.

        Called after a collector failed: its marks for records that were not
        emitted yet must not be saved, or the next run would skip those items.
        The next use of such a store reloads its saved state.

        Returns:
            int: Number of dropped stores.
        """
        dirty = [key for key, store in self._seen.items() if store.dirty]
        for key in dirty:
            del self._seen[key]
        return len(dirty)


class Collector(ABC):
    """A source family that produces records of one or more datasets.

    Class attributes:
        name: Name used in settings, state and the run log.
        datasets: Datasets the collector writes.
        min_days_between_runs: Days that must pass after the last complete run
            before the collector runs again; 0 runs it in every run. Applies only
            to collectors whose data can be fetched later without loss.
    """
    name: ClassVar[str]
    datasets: ClassVar[tuple[str, ...]]
    min_days_between_runs: ClassVar[int] = 0

    @abstractmethod
    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Collects new and changed items and emits them through ``ctx.emit``.

        Args:
            ctx: The run context.
            entry: Run history of this collector (first run date, previous cursor).

        Returns:
            CollectorResult: New cursor and counters.

        Raises:
            CollectorSkipped: If a prerequisite is missing.
        """
