"""
Executes one pipeline run.

The collectors run one after another in COLLECTOR_ORDER: first the
registries (series catalog, regions, snapshot configuration), which send no
requests, then the snapshots of the volatile sources (files of websites,
GENESIS tables), because these sources do not keep what they published, and
last the statistical series of the ECB and of Eurostat, whose providers keep
the history and can be fetched by a later run. Each collector runs in
isolation: a failure is logged and recorded in ``state/collectors.json`` and
the run log, and the other
collectors continue. The data that a failing collector has already emitted
stays written.

The run has a deadline (RUN_DEADLINE_MINUTES): the collectors start no new
work after it, and a collector that has not started by then is skipped, so
that the workflow commits the data before its timeout. Collectors with
``min_days_between_runs`` run only when that many days have passed since
their last complete run. A backfill run fetches every catalog entry, also
those whose dataset the provider reports as unchanged, and ignores these
intervals.
"""
import logging
import traceback
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from collectors.base import Collector, CollectorSkipped, RunContext, utc_now
from collectors.registries import RegionsCollector, SeriesCatalogCollector, SnapshotCatalogCollector
from collectors.sdmx import EcbCollector, EurostatCollector
from collectors.snapshots import FilesCollector, GenesisCollector
from settings import Settings
from state import CollectorStates, append_run_log
from web import WebClient

logger = logging.getLogger(__name__)

COLLECTOR_ORDER: tuple[str, ...] = ("series_catalog", "regions", "snapshot_catalog", "files", "genesis", "ecb",
                                    "eurostat")
# Request intervals per host in seconds; hosts not listed get one request per second.
HOST_INTERVALS: dict[str, float] = {}
# Outcomes that make the run exit with code 1
FAILED_STATUSES = frozenset({"failed", "skipped"})


@dataclass
class CollectorOutcome:
    """Result of one collector in a run.

    Attributes:
        status (str): ok; partial if the collector stopped at the run deadline or a rate limit;
            not_due if it is not due in this run; failed or skipped.
        records (dict[str, int]): Records written per dataset.
        counters (dict): Counters reported by the collector.
        error (Optional[str]): Error message for failed and skipped collectors.
    """
    status: str
    records: dict[str, int] = field(default_factory=dict)
    counters: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class RunSummary:
    """Summary of a run, written to the run log.

    Attributes:
        started (datetime): Start of the run (UTC).
        finished (Optional[datetime]): End of the run (UTC).
        mode (str): normal, backfill or dry-run.
        collectors (dict[str, CollectorOutcome]): Outcome per collector.
        requests (int): HTTP requests sent.
    """
    started: datetime
    finished: Optional[datetime] = None
    mode: str = "normal"
    collectors: dict[str, CollectorOutcome] = field(default_factory=dict)
    requests: int = 0

    @property
    def failed(self) -> list[str]:
        """Names of the collectors that failed or were skipped."""
        return [name for name, outcome in self.collectors.items() if outcome.status in FAILED_STATUSES]

    def to_log_entry(self) -> dict[str, Any]:
        """Returns the JSON-compatible run log line."""
        return {
            "started": self.started.isoformat(),
            "finished": self.finished.isoformat() if self.finished else None,
            "mode": self.mode,
            "requests": self.requests,
            "collectors": {
                name: {key: value for key, value in vars(outcome).items() if value not in (None, {}, [])}
                for name, outcome in self.collectors.items()
            },
        }


def build_collectors() -> list[Collector]:
    """Creates all collectors in COLLECTOR_ORDER.

    Returns:
        list[Collector]: The collectors.
    """
    collectors: dict[str, Collector] = {
        collector.name: collector for collector in (
            SeriesCatalogCollector(), RegionsCollector(), SnapshotCatalogCollector(), FilesCollector(),
            GenesisCollector(), EcbCollector(), EurostatCollector(),
        )
    }
    return [collectors[name] for name in COLLECTOR_ORDER]


def run_pipeline(
    settings: Settings,
    collectors: Sequence[Collector],
    *,
    dry_run: bool = False,
    backfill: bool = False,
    web: Optional[WebClient] = None,
    clock: Callable[[], datetime] = utc_now,
) -> RunSummary:
    """Runs the selected collectors once.

    Args:
        settings: The run settings; ``settings.collectors`` restricts the collectors.
            Explicitly selected collectors run even if they are not due.
        collectors: The available collectors, in the order of execution.
        dry_run: If True, nothing is written.
        backfill: If True, every catalog entry is fetched and the run intervals are ignored.
        web: HTTP client; created with HOST_INTERVALS if omitted.
        clock: Returns the current UTC time.

    Returns:
        RunSummary: Outcome per collector.
    """
    started = clock()
    own_client = web is None
    client = web or WebClient(host_intervals=dict(HOST_INTERVALS))
    states = CollectorStates.load(settings.state_dir)
    ctx = RunContext(settings, client, started, dry_run=dry_run, clock=clock, backfill=backfill)
    mode = "dry-run" if dry_run else ("backfill" if backfill else "normal")
    summary = RunSummary(started=started, mode=mode)
    try:
        for collector in collectors:
            if settings.collectors and collector.name not in settings.collectors:
                continue
            if _not_due(ctx, states, collector):
                summary.collectors[collector.name] = CollectorOutcome(status="not_due")
                continue
            summary.collectors[collector.name] = _run_collector(ctx, states, collector, started)
            ctx.save_seen()
            if not dry_run:
                states.save()
    finally:
        if own_client:
            client.close()
    summary.finished = clock()
    summary.requests = client.request_count
    if not dry_run:
        append_run_log(settings.state_dir, summary.to_log_entry())
    return summary


def _not_due(ctx: RunContext, states: CollectorStates, collector: Collector) -> bool:
    """Tells whether a collector skips this run because its last complete run is too recent.

    Backfill runs and explicitly selected collectors are always due.
    """
    if collector.min_days_between_runs <= 0 or ctx.backfill or ctx.settings.collectors:
        return False
    last_complete = states.entry(collector.name, ctx.today).last_complete
    if last_complete is None or (ctx.today - last_complete.date()).days >= collector.min_days_between_runs:
        return False
    logger.info("Collector %s is not due: last complete run on %s, runs every %d days.",
                collector.name, last_complete.date().isoformat(), collector.min_days_between_runs)
    return True


def _run_collector(ctx: RunContext, states: CollectorStates, collector: Collector, started: datetime) -> CollectorOutcome:
    """Runs one collector and records its outcome in the collector states."""
    entry = states.entry(collector.name, ctx.today)
    before = Counter(ctx.written)
    if ctx.past_deadline():
        message = f"run deadline of {ctx.settings.run_deadline_minutes} minutes reached before the collector started"
        logger.error("Collector %s skipped: %s", collector.name, message)
        states.record_failure(collector.name, started, f"skipped: {message}")
        return CollectorOutcome(status="skipped", error=message)
    logger.info("Collector %s started.", collector.name)
    try:
        result = collector.collect(ctx, entry)
    except CollectorSkipped as e:
        logger.error("Collector %s skipped: %s", collector.name, e)
        ctx.discard_unsaved()
        states.record_failure(collector.name, started, f"skipped: {e}")
        return CollectorOutcome(status="skipped", error=str(e))
    # Isolation boundary: an unexpected error of one collector must not stop the others.
    except Exception as e:
        logger.error("Collector %s failed: %s\n%s", collector.name, e, traceback.format_exc())
        # Marks of records that were not emitted yet must not be saved
        ctx.discard_unsaved()
        states.record_failure(collector.name, started, f"{e.__class__.__name__}: {e}")
        written = {name: count - before[name] for name, count in ctx.written.items() if count - before[name]}
        return CollectorOutcome(status="failed", records=written, error=f"{e.__class__.__name__}: {e}")
    written = {name: count - before[name] for name, count in ctx.written.items() if count - before[name]}
    states.record_success(collector.name, started, result.cursor, complete=result.complete)
    if result.error:
        states.record_failure(collector.name, started, result.error)
        logger.error("Collector %s finished with an error: %s", collector.name, result.error)
        return CollectorOutcome(status="failed", records=written, counters=result.counters, error=result.error)
    if not result.complete:
        logger.warning("Collector %s stopped early: %s records; the rest follows in the next run.",
                       collector.name, written or "no new")
        return CollectorOutcome(status="partial", records=written, counters=result.counters)
    logger.info("Collector %s finished: %s records.", collector.name, written or "no new")
    return CollectorOutcome(status="ok", records=written, counters=result.counters)
