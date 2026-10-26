"""Tests of the run orchestration with fake collectors."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from collectors.base import Collector, CollectorResult, CollectorSkipped, RunContext
from conftest import RUN_STARTED, read_dataset
from pipeline import COLLECTOR_ORDER, build_collectors, run_pipeline
from schema import RegionRecord
from settings import Settings
from state import CollectorEntry, CollectorStates


class WritingCollector(Collector):
    """Writes one region record."""
    name = "writer"
    datasets = ("regions",)

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        ctx.emit("regions", [RegionRecord(
            **ctx.envelope("regions", "regions:DE21", revision=0, source_id="registry", source_url=None,
                           license="test", backfilled=False),
            geo="DE21", name="Oberbayern", level="nuts2", parent="DE2", state="DE-BY", nuts_versions=["2024"],
            added=RUN_STARTED.date(), removed=None, removed_reason=None,
        )])
        return CollectorResult(cursor={"position": 1}, counters={"written": 1})


class FailingCollector(Collector):
    """Raises an unexpected error."""
    name = "failing"
    datasets = ()

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        raise RuntimeError("unexpected")


class SkippedCollector(Collector):
    """Misses a prerequisite."""
    name = "skipped"
    datasets = ()

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        raise CollectorSkipped("configuration missing")


class WeeklyCollector(WritingCollector):
    """Writes one region record and runs at most once a week."""
    name = "weekly"
    min_days_between_runs = 6


class PartialCollector(Collector):
    """Stops at the run deadline in every run."""
    name = "partial"
    datasets = ()
    min_days_between_runs = 6

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        return CollectorResult(complete=False)


class SlowCollector(Collector):
    """Advances a mutable clock by 30 minutes."""
    name = "slow"
    datasets = ()

    def __init__(self, now: list[datetime]) -> None:
        self._now = now

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        self._now[0] += timedelta(minutes=30)
        return CollectorResult()


def settings_for(tmp_path, **overrides) -> Settings:
    return replace(Settings(), output_dir=tmp_path / "data", **overrides)


def test_failing_collector_does_not_stop_the_others(tmp_path, fake_web) -> None:
    settings = settings_for(tmp_path)

    summary = run_pipeline(settings, [FailingCollector(), SkippedCollector(), WritingCollector()],
                           web=fake_web.client(), clock=lambda: RUN_STARTED)

    assert summary.collectors["failing"].status == "failed"
    assert summary.collectors["skipped"].status == "skipped"
    assert summary.collectors["writer"].status == "ok"
    assert summary.collectors["writer"].records == {"regions": 1}
    assert sorted(summary.failed) == ["failing", "skipped"]
    assert len(read_dataset(settings.output_dir, "regions")) == 1

    states = CollectorStates.load(settings.state_dir)
    assert states.entry("failing", RUN_STARTED.date()).consecutive_failures == 1
    assert states.entry("writer", RUN_STARTED.date()).cursor == {"position": 1}
    log = [json.loads(line) for line in (settings.state_dir / "run_log.jsonl").read_text().splitlines()]
    assert log[-1]["collectors"]["failing"]["error"] == "RuntimeError: unexpected"


def test_collector_selection_and_dry_run(tmp_path, fake_web) -> None:
    settings = settings_for(tmp_path, collectors=("writer",))

    summary = run_pipeline(settings, [FailingCollector(), WritingCollector()], dry_run=True,
                           web=fake_web.client(), clock=lambda: datetime(2026, 10, 20, tzinfo=timezone.utc))

    assert list(summary.collectors) == ["writer"]
    assert summary.mode == "dry-run"
    assert not settings.output_dir.exists()


def test_weekly_collector_runs_six_days_after_its_last_complete_run(tmp_path, fake_web) -> None:
    settings = settings_for(tmp_path)

    def run(days: int, **options) -> str:
        summary = run_pipeline(options.pop("settings", settings), [WeeklyCollector()], web=fake_web.client(),
                               clock=lambda: RUN_STARTED + timedelta(days=days), **options)
        return summary.collectors["weekly"].status

    assert run(0) == "ok"
    assert run(3) == "not_due"
    assert run(7) == "ok"
    # Explicitly selected collectors and backfill runs ignore the interval
    assert run(8, settings=replace(settings, collectors=("weekly",))) == "ok"
    assert run(9, backfill=True) == "ok"
    log = [json.loads(line) for line in (settings.state_dir / "run_log.jsonl").read_text().splitlines()]
    assert log[1]["collectors"]["weekly"] == {"status": "not_due"}
    assert log[-1]["mode"] == "backfill"


def test_collector_stopped_at_the_deadline_is_partial_and_stays_due(tmp_path, fake_web) -> None:
    settings = settings_for(tmp_path)

    first = run_pipeline(settings, [PartialCollector()], web=fake_web.client(), clock=lambda: RUN_STARTED)
    second = run_pipeline(settings, [PartialCollector()], web=fake_web.client(),
                          clock=lambda: RUN_STARTED + timedelta(days=3))

    assert first.collectors["partial"].status == "partial" and first.failed == []
    assert second.collectors["partial"].status == "partial"
    entry = CollectorStates.load(settings.state_dir).entry("partial", RUN_STARTED.date())
    assert entry.last_success is not None and entry.last_complete is None


def test_collectors_after_the_run_deadline_are_skipped(tmp_path, fake_web) -> None:
    settings = settings_for(tmp_path)
    now = [RUN_STARTED]

    summary = run_pipeline(settings, [SlowCollector(now), WritingCollector()], web=fake_web.client(),
                           clock=lambda: now[0])

    assert summary.collectors["slow"].status == "ok"
    assert summary.collectors["writer"].status == "skipped"
    assert "run deadline" in (summary.collectors["writer"].error or "")
    assert summary.failed == ["writer"]
    assert not (settings.output_dir / "regions").exists()


def test_build_collectors_follows_the_order() -> None:
    assert [collector.name for collector in build_collectors()] == list(COLLECTOR_ORDER)
