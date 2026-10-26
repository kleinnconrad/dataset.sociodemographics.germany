"""Tests of the partitioned storage and the state files."""
import json
from datetime import date, datetime, timezone

from schema import RegionRecord
from state import CollectorStates, SeenStore, content_hash, is_backfilled, seen_path
from storage import dumps_records, iter_records, partition_file, write_records

DAY = date(2026, 10, 26)


def _record(record_id: str, revision: int = 0, name: str = "Oberbayern") -> RegionRecord:
    return RegionRecord(
        record_id=record_id, revision=revision, dataset="regions",
        observed_at=datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc), source_id="registry",
        source_url=None, license="test", backfilled=False, geo="DE21", name=name, level="nuts2", parent="DE2",
        state="DE-BY", nuts_versions=["2021", "2024"], added=DAY, removed=None, removed_reason=None,
    )


def test_partition_file_uses_hive_style_folders(tmp_path) -> None:
    path = partition_file(tmp_path, "regions", DAY)
    assert path == tmp_path / "regions" / "year=2026" / "month=10" / "regions_2026-10-26.json"


def test_dumps_records_writes_one_record_per_line() -> None:
    text = dumps_records([{"a": 1}, {"b": "ä"}])
    assert text == '[\n{"a":1},\n{"b":"ä"}\n]\n'
    assert json.loads(text) == [{"a": 1}, {"b": "ä"}]


def test_write_records_merges_by_record_id_and_revision(tmp_path) -> None:
    write_records(tmp_path, "regions", DAY, [_record("a"), _record("b")])
    write_records(tmp_path, "regions", DAY, [_record("a", name="x"), _record("a", revision=1, name="y")])

    records = list(iter_records(tmp_path, "regions"))
    keys = sorted((r["record_id"], r["revision"], r["name"]) for r in records)
    assert keys == [("a", 0, "x"), ("a", 1, "y"), ("b", 0, "Oberbayern")]


def test_write_records_without_records_writes_nothing(tmp_path) -> None:
    assert write_records(tmp_path, "regions", DAY, []) is None
    assert not (tmp_path / "regions").exists()


def test_seen_store_classifies_and_counts_revisions(tmp_path) -> None:
    store = SeenStore.load(tmp_path, "regions")
    assert store.classify("1", "a") == "new"
    assert store.mark("1", "a", DAY) == 0
    assert store.classify("1", "a") == "unchanged"
    assert store.mark("1", "a", DAY) == 0
    assert store.classify("1", "b") == "changed"
    assert store.mark("1", "b", DAY, data={"status": "x"}) == 1
    store.save()

    reloaded = SeenStore.load(tmp_path, "regions")
    entry = reloaded.get("1")
    assert entry is not None and entry.revision == 1 and entry.data == {"status": "x"}


def test_seen_store_is_written_only_after_changes(tmp_path) -> None:
    store = SeenStore.load(tmp_path, "regions")
    store.save()
    assert not seen_path(tmp_path, "regions").exists()

    store.mark("1", "a", DAY)
    store.save()
    path = seen_path(tmp_path, "regions")
    written = path.stat().st_mtime_ns
    SeenStore.load(tmp_path, "regions").save()
    assert path.stat().st_mtime_ns == written


def test_sharded_seen_stores_use_one_file_per_shard(tmp_path) -> None:
    store = SeenStore.load(tmp_path, "sociodemographic_series", shard="population/DE1")
    store.mark("population.DE11.T.TOTAL", "h", DAY)
    store.save()

    assert seen_path(tmp_path, "sociodemographic_series", "population/DE1") == (
        tmp_path / "seen" / "sociodemographic_series" / "population" / "DE1.json")
    assert "population.DE11.T.TOTAL" in SeenStore.load(tmp_path, "sociodemographic_series", shard="population/DE1")
    assert len(SeenStore.load(tmp_path, "sociodemographic_series", shard="population/DE2")) == 0


def test_seen_store_prunes_old_entries(tmp_path) -> None:
    store = SeenStore.load(tmp_path, "series_catalog")
    store.mark("old", "h", date(2026, 8, 1))
    store.mark("new", "h", date(2026, 10, 19))

    assert store.prune(DAY, retention_days=30) == 1
    assert "old" not in store and "new" in store


def test_collector_states_track_failures_and_cursor(tmp_path) -> None:
    states = CollectorStates.load(tmp_path)
    started = datetime(2026, 10, 26, 2, 17, tzinfo=timezone.utc)
    assert states.entry("sociodemographic_series", DAY).first_run == DAY
    states.record_failure("sociodemographic_series", started, "boom")
    states.record_failure("sociodemographic_series", started, "boom")
    assert states.entry("sociodemographic_series", DAY).consecutive_failures == 2
    states.record_success("sociodemographic_series", started, {"last_modified": "x"})
    states.save()

    entry = CollectorStates.load(tmp_path).entry("sociodemographic_series", date(2027, 1, 1))
    assert entry.consecutive_failures == 0
    assert entry.cursor == {"last_modified": "x"}
    assert entry.first_run == DAY


def test_is_backfilled_uses_a_grace_period() -> None:
    assert is_backfilled(date(2026, 10, 1), first_run=DAY)
    assert not is_backfilled(date(2026, 10, 20), first_run=DAY)
    assert not is_backfilled(None, first_run=DAY)


def test_content_hash_ignores_key_order() -> None:
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
