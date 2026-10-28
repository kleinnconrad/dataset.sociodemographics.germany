"""Tests of the README overview generator."""
from datetime import datetime, timezone

import pytest

from conftest import CATALOG_FIXTURE, REGIONS_FIXTURE, SOURCES_FIXTURE
from generate_dataset_overview import END_MARKER, START_MARKER, build_overview, update_readme
from state import append_run_log
from storage import dumps_records, partition_file

NOW = datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)


def write(data_dir, dataset: str, records: list[dict]) -> None:
    path = partition_file(data_dir, dataset, NOW.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_records(records), encoding="utf-8")


def overview_of(data_dir) -> str:
    return build_overview(data_dir, now=NOW, catalog_path=CATALOG_FIXTURE, regions_path=REGIONS_FIXTURE,
                          sources_path=SOURCES_FIXTURE)


def test_build_overview_counts_records_series_and_regions(tmp_path) -> None:
    data_dir = tmp_path / "data"
    write(data_dir, "sociodemographic_series", [
        {"record_id": "series:population.DE.F", "revision": 0, "series_id": "population.DE.F",
         "concept": "population", "geo_level": "country", "observed_at": "2026-10-27T01:00:00Z",
         "last_period": "2025", "values": {"2024": 1.0, "2025": 2.0}},
        {"record_id": "series:population.DE212.F", "revision": 0, "series_id": "population.DE212.F",
         "concept": "population", "geo_level": "nuts3", "observed_at": "2026-10-27T01:00:00Z",
         "last_period": "2024", "values": {"2024": 1.0}},
        {"record_id": "series:population.DE212.F", "revision": 1, "series_id": "population.DE212.F",
         "concept": "population", "geo_level": "nuts3", "observed_at": "2026-10-27T01:00:00Z",
         "last_period": "2025", "values": {"2025": 1.0}},
    ])
    append_run_log(data_dir / "state", {"started": "2026-10-27T01:07:00+00:00", "mode": "normal",
                                        "collectors": {"eurostat": {"status": "ok",
                                                                    "records": {"sociodemographic_series": 3}}}})
    overview = overview_of(data_dir)
    assert "| `sociodemographic_series` | 3 | 2 | 1 | 2026-10-27 to 2026-10-27 |" in overview
    assert "- **Latest run:** 2026-10-27 01:07 UTC (normal)" in overview
    assert "- **Active catalog entries:** 4 (eurostat 3, ecb 1); with stored series: 1; stored series: 2" in overview
    assert ("| `population` | population structure | A | persons on 1 January | eurostat `demo_r_pjanaggr3` | 2 | "
            "country, nuts3 | 2025 | 1 |") in overview
    assert "- **Registered regions:** 7 (Germany 1, states 2, government regions 2, districts 2)" in overview
    assert "<summary><code>sociodemographic_series</code>" in overview


def test_overview_of_an_empty_dataset(tmp_path) -> None:
    overview = overview_of(tmp_path / "data")
    assert "- No run yet." in overview and "**Fields**" not in overview


def test_update_readme_replaces_only_the_marked_part(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(f"# Title\n{START_MARKER}\nold\n{END_MARKER}\nrest\n", encoding="utf-8")
    assert update_readme(readme, "new")
    assert readme.read_text(encoding="utf-8") == f"# Title\n{START_MARKER}\nnew\n{END_MARKER}\nrest\n"
    assert not update_readme(readme, "new")
    readme.write_text("no markers", encoding="utf-8")
    with pytest.raises(ValueError):
        update_readme(readme, "new")


def test_overview_lists_press_sources_and_annotation_topics(tmp_path) -> None:
    data_dir = tmp_path / "data"
    write(data_dir, "news_items", [
        {"record_id": f"news:{n}", "source_id": "news_example", "outlet_id": "news_example",
         "observed_at": "2026-10-27T01:30:00Z", "published_at": "2026-10-26T20:00:00Z",
         "content_basis": "lead_text" if n == 0 else "title_only"} for n in range(3)
    ])
    write(data_dir, "press_releases", [
        {"record_id": "press:1", "source_id": "stat_office", "press_source_id": "stat_office",
         "observed_at": "2026-10-27T01:30:00Z", "published_at": "2026-10-25T08:00:00Z", "content_basis": "title_only"},
    ])
    write(data_dir, "text_annotations", [
        {"record_id": "annotation:news:0:h", "item_id": "news:0", "is_sociodemographic": True,
         "topic": "Births and fertility", "observed_at": "2026-10-27T01:30:00Z"},
        {"record_id": "annotation:press:1:h", "item_id": "press:1", "is_sociodemographic": True,
         "topic": "Population and demographic change", "observed_at": "2026-10-27T01:30:00Z"},
    ])

    overview = overview_of(data_dir)

    assert "- **News outlets:** 2 configured (2 active)" in overview
    assert "- **Press-release sources:** 2 configured (2 active)" in overview
    assert "| News Example | national daily | active | 3 | 3 | 33.3% (1) | 33.3% (1) | 2026-10-26 |" in overview
    assert "- **Annotated items:** 2 of 4 (50.0%)" in overview
    assert "| Births and fertility | 1 | 50.0% |" in overview
