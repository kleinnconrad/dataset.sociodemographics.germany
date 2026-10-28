"""Tests of the collectors that write the series catalog, the region registry and the press configuration."""
from collectors.registries import PressCatalogCollector, RegionsCollector, SeriesCatalogCollector
from conftest import CATALOG_FIXTURE, REGIONS_FIXTURE, SOURCES_FIXTURE, read_dataset
from schema import PressCatalogRecord, RegionRecord, SeriesCatalogRecord
from state import CollectorEntry


def test_catalog_entries_are_written_once_and_again_after_a_change(make_ctx, tmp_path) -> None:
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    result = SeriesCatalogCollector(CATALOG_FIXTURE).collect(ctx, entry)
    assert result.counters == {"items": 5, "new": 5, "changed": 0}
    records = {record["concept"]: record for record in read_dataset(tmp_path / "data", "series_catalog")}
    for record in records.values():
        SeriesCatalogRecord.model_validate(record)
    assert records["population"]["breakdown"] == {"sex": ["M", "F"]}
    assert records["population"]["key_template"] == "A.NR.{sex}.TOTAL.{geo}"
    assert records["total_fertility_rate"]["extra_geos"] == ["EU27_2020"]
    assert records["old_concept"]["removed"] == "2026-10-27"

    assert SeriesCatalogCollector(CATALOG_FIXTURE).collect(make_ctx(), entry).counters["new"] == 0
    changed = tmp_path / "series.toml"
    changed.write_text(CATALOG_FIXTURE.read_text(encoding="utf-8").replace('unit = "live births per woman"',
                                                                             'unit = "children per woman"'),
                       encoding="utf-8")
    assert SeriesCatalogCollector(changed).collect(make_ctx(), entry).counters["changed"] == 1
    revisions = [r for r in read_dataset(tmp_path / "data", "series_catalog") if r["revision"] == 1]
    assert [r["concept"] for r in revisions] == ["total_fertility_rate"]


def test_regions_are_written_with_level_parent_and_state(make_ctx, tmp_path) -> None:
    ctx = make_ctx()
    result = RegionsCollector(REGIONS_FIXTURE).collect(ctx, CollectorEntry(first_run=ctx.today))
    assert result.counters["new"] == 8
    records = {record["geo"]: record for record in read_dataset(tmp_path / "data", "regions")}
    for record in records.values():
        RegionRecord.model_validate(record)
    assert (records["DE212"]["level"], records["DE212"]["parent"], records["DE212"]["state"]) == ("nuts3", "DE21",
                                                                                               "DE-BY")
    assert records["DE"]["parent"] is None and records["DE"]["state"] is None
    assert records["DE213"]["removed_reason"] == "test of a removed region"


def test_press_sources_are_written_with_their_attributes(make_ctx, tmp_path) -> None:
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    result = PressCatalogCollector(SOURCES_FIXTURE).collect(ctx, entry)
    assert result.counters == {"items": 4, "new": 4, "changed": 0}
    records = {record["press_id"]: record for record in read_dataset(tmp_path / "data", "press_catalog")}
    for record in records.values():
        PressCatalogRecord.model_validate(record)
    assert records["news_example"]["kind"] == "outlet" and records["news_example"]["topic_filter"] is True
    assert records["news_example"]["news_sitemap"] == "https://news.example/sitemap-news.xml"
    assert records["school_example"]["outlet_topic"] == "Education and schools"
    assert records["school_example"]["topic_filter"] is False
    assert records["stat_office"]["actor_type"] == "statistical_office" and records["stat_office"]["outlet_type"] is None
    assert records["government"]["jurisdiction"] == "DE-BY" and records["government"]["topic_filter"] is True

    assert PressCatalogCollector(SOURCES_FIXTURE).collect(make_ctx(), entry).counters["new"] == 0
