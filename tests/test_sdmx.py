"""Tests of the SDMX collectors: parsing, grouped requests, vintages and skipping of unchanged datasets."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from catalog import load_catalog
from collectors.sdmx import (
    ECB,
    EUROSTAT,
    EUROSTAT_TOC_URL,
    EcbCollector,
    EurostatCollector,
    fetch_entry,
    parse_sdmx_csv,
    parse_toc,
)
from conftest import CATALOG_FIXTURE, FIXTURES_DIR, REGIONS_FIXTURE, RUN_STARTED, read_dataset
from regions import active_regions
from schema import SociodemographicSeriesRecord
from state import CollectorEntry
from web import FetchError, NotFoundError

POPULATION_CSV = (FIXTURES_DIR / "sdmx" / "population.csv").read_text(encoding="utf-8")


def entry_of(concept: str):
    return next(entry for entry in load_catalog(CATALOG_FIXTURE) if entry.concept == concept)


def population_url(geos: list[str]) -> str:
    entry = entry_of("population")
    return EUROSTAT.url(entry.flow, entry.request_key(geos))


ALL_GEOS = ["DE", "DE1", "DE11", "DE111", "DE2", "DE21", "DE212"]


def test_parse_sdmx_csv_matches_the_dimensions_by_position() -> None:
    series, unexpected, last_update = parse_sdmx_csv(POPULATION_CSV, entry_of("population"), ALL_GEOS)
    assert series[("DE", ("F",))] == {"2024": 42500000.0, "2025": 42600000.0}
    # An empty value is skipped; the row with the code T was not requested
    assert series[("DE111", ("M",))] == {"2024": 318000.0}
    assert unexpected == 1
    assert last_update == "14/03/26 23:00:00"


def test_parse_sdmx_csv_reads_ecb_csvdata_with_a_key_column() -> None:
    text = (FIXTURES_DIR / "sdmx" / "net_wealth.csv").read_text(encoding="utf-8")
    series, unexpected, _ = parse_sdmx_csv(text, entry_of("net_wealth_share"), ["DE"])
    assert series[("DE", ("T10",))] == {"2025-Q4": 56.1, "2026-Q1": 56.3}
    assert series[("DE", ("B50",))] == {"2026-Q1": 2.9}
    assert unexpected == 0


def test_parse_sdmx_csv_rejects_another_number_of_dimensions() -> None:
    text = "DATAFLOW,LAST UPDATE,freq,unit,geo,TIME_PERIOD,OBS_VALUE\nX,,A,NR,DE,2024,1\n"
    with pytest.raises(ValueError, match="3 dimensions"):
        parse_sdmx_csv(text, entry_of("population"), ["DE"])


def test_parse_sdmx_csv_rejects_another_fixed_code() -> None:
    text = POPULATION_CSV.replace(",A,NR,F,TOTAL,DE,2024,", ",A,PC,F,TOTAL,DE,2024,")
    with pytest.raises(ValueError, match="column unit has the code 'PC'"):
        parse_sdmx_csv(text, entry_of("population"), ALL_GEOS)


def test_fetch_entry_groups_the_areas_and_counts_groups_without_data(fake_web) -> None:
    provider = replace(EUROSTAT, max_geos=4)
    fake_web.add(population_url(ALL_GEOS[:4]), POPULATION_CSV, content_type="text/csv")
    data = fetch_entry(fake_web.client(), provider, entry_of("population"), ALL_GEOS)
    assert (data.requests, data.empty_groups) == (2, 1)
    assert sorted(key.series_id for key in data.series) == ["population.DE.F", "population.DE.M",
                                                           "population.DE111.F", "population.DE111.M"]
    key = next(key for key in data.series if key.series_id == "population.DE111.F")
    assert data.series[key].url.endswith("/data/demo_r_pjanaggr3/A.NR.F.TOTAL.DE111")
    assert data.series[key].source_updated is not None


def test_fetch_entry_leaves_out_areas_that_the_dataset_does_not_know(fake_web) -> None:
    fake_web.add(population_url(ALL_GEOS), "invalid code DE212", status=400, content_type="text/plain")
    header = POPULATION_CSV.split("\n", 1)[0]
    fake_web.add(EUROSTAT.url("demo_r_pjanaggr3", "A.NR.M.TOTAL."),
                 header + "\nX,,A,NR,M,TOTAL,DE,2025,1,\nX,,A,NR,M,TOTAL,DE111,2025,1,\nX,,A,NR,M,TOTAL,FR1,2025,1,\n",
                 content_type="text/csv")
    fake_web.add(population_url(["DE", "DE111"]), POPULATION_CSV, content_type="text/csv")
    data = fetch_entry(fake_web.client(), EUROSTAT, entry_of("population"), ALL_GEOS)
    assert data.unknown_geos == ["DE1", "DE11", "DE2", "DE21", "DE212"]
    assert sorted(key.series_id for key in data.series) == ["population.DE.F", "population.DE.M",
                                                           "population.DE111.F", "population.DE111.M"]


def test_fetch_entry_keeps_a_bad_request_that_is_not_caused_by_areas(fake_web) -> None:
    fake_web.add(population_url(ALL_GEOS), "invalid code", status=400, content_type="text/plain")
    with pytest.raises(FetchError, match="HTTP 400"):
        fetch_entry(fake_web.client(), EUROSTAT, entry_of("population"), ALL_GEOS)


def test_fetch_entry_without_data_in_any_group_raises_not_found(fake_web) -> None:
    with pytest.raises(NotFoundError):
        fetch_entry(fake_web.client(), EUROSTAT, entry_of("population"), ALL_GEOS)


def test_parse_toc_reads_datasets_and_skips_folders() -> None:
    toc = parse_toc((FIXTURES_DIR / "sdmx" / "toc.txt").read_text(encoding="utf-8"))
    assert "data" not in toc
    item = toc["demo_r_pjanaggr3"]
    assert item.title == "Population on 1 January by broad age group, sex and NUTS 3 region"
    assert item.last_update == date(2026, 3, 14) and (item.data_start, item.data_end) == ("1990", "2025")


def test_parse_toc_rejects_an_unknown_format() -> None:
    with pytest.raises(ValueError, match="unexpected header"):
        parse_toc("a\tb\n1\t2\n")


def collector() -> EurostatCollector:
    return EurostatCollector(CATALOG_FIXTURE, REGIONS_FIXTURE)


def test_eurostat_collector_writes_vintages_and_only_changed_periods_later(fake_web, make_ctx, tmp_path) -> None:
    fake_web.add(population_url(ALL_GEOS), POPULATION_CSV, content_type="text/csv")
    ctx = make_ctx()
    result = collector().collect(ctx, CollectorEntry(first_run=ctx.today))
    assert result.counters["new"] == 6
    assert result.counters["not_found"] == 2 and result.counters["not_found_entries"] == ["total_fertility_rate",
                                                                                         "asylum_applicants"]
    assert result.error is None
    records = {record["series_id"]: record for record in read_dataset(tmp_path / "data", "sociodemographic_series")}
    record = records["population.DE212.F"]
    SociodemographicSeriesRecord.model_validate(record)
    assert record["breakdown"] == {"sex": "F"}
    assert (record["geo_level"], record["state"], record["provider_key"]) == ("nuts3", "DE-BY", "A.NR.F.TOTAL.DE212")
    assert record["values"] == {"2024": 772000.0} and record["backfilled"] is True
    assert records["population.DE.M"]["state"] is None

    # A revised and an added value: the next revision holds only these periods
    revised = POPULATION_CSV.replace("DE212,2024,772000", "DE212,2024,772100") + (
        "ESTAT:DEMO_R_PJANAGGR3(1.0),14/03/26 23:00:00,A,NR,F,TOTAL,DE212,2025,775000,\n")
    fake_web.add(population_url(ALL_GEOS), revised, content_type="text/csv")
    later = make_ctx(started=RUN_STARTED + timedelta(days=3))
    result = collector().collect(later, CollectorEntry(first_run=ctx.today, cursor=result.cursor))
    assert (result.counters["new"], result.counters["revised"], result.counters["unchanged"]) == (0, 1, 5)
    revision = [record for record in read_dataset(tmp_path / "data", "sociodemographic_series")
                if record["revision"] == 1]
    assert [(record["series_id"], record["values"], record["observations"]) for record in revision] == [
        ("population.DE212.F", {"2024": 772100.0, "2025": 775000.0}, 2)]


def test_unchanged_datasets_are_skipped_until_the_full_check(fake_web, make_ctx) -> None:
    fake_web.add(population_url(ALL_GEOS), POPULATION_CSV, content_type="text/csv")
    fake_web.add(EUROSTAT_TOC_URL, (FIXTURES_DIR / "sdmx" / "toc.txt").read_text(encoding="utf-8"),
                 content_type="text/plain")
    ctx = make_ctx()
    first = collector().collect(ctx, CollectorEntry(first_run=ctx.today))
    assert first.cursor["entries"]["population"]["dataset_updated"] == "2026-03-14"

    second = collector().collect(make_ctx(started=RUN_STARTED + timedelta(days=3)),
                                 CollectorEntry(first_run=ctx.today, cursor=first.cursor))
    assert second.counters["unchanged_datasets"] == 1 and second.counters["requests"] == 0

    after_full_check_days = make_ctx(started=RUN_STARTED + timedelta(days=30))
    third = collector().collect(after_full_check_days, CollectorEntry(first_run=ctx.today, cursor=first.cursor))
    assert third.counters["unchanged_datasets"] == 0 and third.counters["unchanged"] == 6

    backfill = collector().collect(make_ctx(backfill=True, started=RUN_STARTED + timedelta(days=4)),
                                   CollectorEntry(first_run=ctx.today, cursor=first.cursor))
    assert backfill.counters["unchanged_datasets"] == 0


def test_incomplete_response_is_not_written(fake_web, make_ctx) -> None:
    long_series = "".join(f"ESTAT:X,,A,NR,F,TOTAL,DE,{year},{year},\n" for year in range(1990, 2026))
    header = POPULATION_CSV.split("\n", 1)[0] + "\n"
    fake_web.add(population_url(ALL_GEOS), header + long_series, content_type="text/csv")
    ctx = make_ctx()
    first = collector().collect(ctx, CollectorEntry(first_run=ctx.today))
    assert first.counters["new"] == 1

    fake_web.add(population_url(ALL_GEOS), header + long_series.split("\n", 30)[-1], content_type="text/csv")
    second = collector().collect(make_ctx(started=RUN_STARTED + timedelta(days=3)),
                                 CollectorEntry(first_run=ctx.today, cursor=first.cursor))
    assert second.counters["incomplete"] == 1 and second.counters["revised"] == 0


def test_all_entries_without_data_are_an_error(fake_web, make_ctx) -> None:
    ctx = make_ctx()
    result = collector().collect(ctx, CollectorEntry(first_run=ctx.today))
    assert result.error == "all 3 entries failed or had no data"


def test_refusal_stops_the_collector(fake_web, make_ctx) -> None:
    fake_web.add(population_url(ALL_GEOS), "Forbidden", status=403, content_type="text/plain")
    ctx = make_ctx()
    result = collector().collect(ctx, CollectorEntry(first_run=ctx.today))
    assert result.error is not None and result.error.startswith("blocked")
    assert result.counters["requests"] == 0


def test_ecb_collector_reads_the_wealth_shares(fake_web, make_ctx, tmp_path) -> None:
    entry = entry_of("net_wealth_share")
    fake_web.add(ECB.url(entry.flow, entry.request_key(["DE"])),
                 (FIXTURES_DIR / "sdmx" / "net_wealth.csv").read_text(encoding="utf-8"), content_type="text/csv")
    ctx = make_ctx()
    result = EcbCollector(CATALOG_FIXTURE, REGIONS_FIXTURE).collect(ctx, CollectorEntry(first_run=ctx.today))
    assert result.counters["new"] == 2
    records = {record["series_id"]: record for record in read_dataset(tmp_path / "data", "sociodemographic_series")}
    assert records["net_wealth_share.DE.T10"]["source_id"] == "ecb_data_portal"
    assert records["net_wealth_share.DE.T10"]["provider_key"] == "Q.DE.S14._Z._Z.NWA.T10.PT.S.N"


def test_catalog_geos_of_the_fixture() -> None:
    regions = active_regions(path=REGIONS_FIXTURE)
    assert entry_of("population").geos(regions) == ALL_GEOS
