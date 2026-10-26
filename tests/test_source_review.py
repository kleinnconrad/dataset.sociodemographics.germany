"""Tests of the monthly source review with a fake web."""
import json
from dataclasses import replace
from datetime import date, datetime, timezone

from catalog import load_catalog
from collectors.sdmx import ECB, EUROSTAT, EUROSTAT_TOC_URL, parse_toc
from conftest import CATALOG_FIXTURE, FIXTURES_DIR, REGIONS_FIXTURE
from providers import load_providers
from regions import active_regions
from settings import Settings
from source_review import (
    EUROSTAT_DSD_URL,
    ReviewResult,
    TermsCheck,
    build_report,
    candidate_datasets,
    check_entry,
    check_terms,
    eurostat_dimensions,
    is_stale,
    page_text,
    period_end,
    possible_successors,
    recent_run_failures,
    run_review,
    stored_coverage,
    unregistered_regions,
)
from state import SeenStore, append_run_log

TODAY = date(2026, 11, 1)
NOW = datetime(2026, 11, 1, 6, 17, tzinfo=timezone.utc)
POPULATION_CSV = (FIXTURES_DIR / "sdmx" / "population.csv").read_text(encoding="utf-8")
TOC = parse_toc((FIXTURES_DIR / "sdmx" / "toc.txt").read_text(encoding="utf-8"))
DSD = """<?xml version="1.0"?><m:Structure xmlns:m="m" xmlns:s="s"><m:Structures><s:DataStructures>
<s:DataStructure id="DEMO_R_FIND3"><s:DataStructureComponents><s:DimensionList id="DimensionDescriptor">
<s:Dimension id="freq" position="1"/><s:Dimension id="indic_de" position="2"/>
<s:Dimension position="3" id="unit"/><s:Dimension id="geo" position="4"/>
<s:TimeDimension id="TIME_PERIOD" position="5"/></s:DimensionList></s:DataStructureComponents>
</s:DataStructure></s:DataStructures></m:Structures></m:Structure>"""


def entry_of(concept: str):
    return next(entry for entry in load_catalog(CATALOG_FIXTURE) if entry.concept == concept)


def test_period_end_and_staleness() -> None:
    assert period_end("2026-02", "M") == date(2026, 2, 28)
    assert period_end("2026-Q4", "Q") == date(2026, 12, 31)
    assert period_end("2026-W01", "W") == date(2026, 1, 4)
    assert period_end("bad", "A") is None
    assert not is_stale("2024", "A", TODAY)
    assert is_stale("2022", "A", TODAY)
    assert is_stale("2026-05", "M", TODAY) and not is_stale("2026-08", "M", TODAY)
    assert is_stale(None, "A", TODAY)


def test_check_entry_reports_coverage_and_gaps(fake_web) -> None:
    entry = entry_of("population")
    regions = active_regions(path=REGIONS_FIXTURE)
    fake_web.add(EUROSTAT.url(entry.flow, entry.request_key(entry.geos(regions))), POPULATION_CSV,
                 content_type="text/csv")
    check = check_entry(fake_web.client(), entry, regions, TODAY)
    assert check.status == "attention"
    assert (check.series, check.expected, check.newest) == (6, 14, "2025")
    assert check.coverage == {"country": (1, 1), "nuts1": (0, 2), "nuts2": (0, 2), "nuts3": (2, 2)}
    assert any("no data for the levels nuts1, nuts2" in problem for problem in check.problems)
    assert any("startPeriod=2023" in url for url in fake_web.requested)
    assert check.example == "population.DE.F 2025: 42,600,000.0"


def test_entry_with_another_dimension_order_is_probed(fake_web) -> None:
    entry = entry_of("total_fertility_rate")
    fake_web.add(EUROSTAT_DSD_URL.format(flow="DEMO_R_FIND3"), DSD, content_type="application/xml")
    check = check_entry(fake_web.client(), entry, active_regions(path=REGIONS_FIXTURE), TODAY)
    assert check.status == "no_data"
    assert check.probe[0] == ("structure: 4 dimensions (freq, indic_de, unit, geo); the key template "
                              "A.NR.TOTFERRT.{geo} has 4 parts")
    assert check.probe[2].startswith("indic_de: no data with the other parts as requested")


ASYLUM_DSD = DSD.replace('<s:Dimension id="freq" position="1"/><s:Dimension id="indic_de" position="2"/>\n'
                         '<s:Dimension position="3" id="unit"/>',
                         '<s:Dimension id="freq" position="1"/><s:Dimension id="unit" position="2"/>'
                         '<s:Dimension id="citizen" position="3"/><s:Dimension id="sex" position="4"/>'
                         '<s:Dimension id="applicant" position="5"/><s:Dimension id="age" position="6"/>'
                         ).replace('id="geo" position="4"', 'id="geo" position="7"')


def test_probe_names_the_requested_codes_that_the_dataset_lacks(fake_web) -> None:
    entry = entry_of("asylum_applicants")
    fake_web.add(EUROSTAT.url(entry.flow, entry.request_key(["DE"])), "invalid code", status=400,
                 content_type="text/plain")
    fake_web.add(EUROSTAT_DSD_URL.format(flow="MIGR_ASYAPPCTZM"), ASYLUM_DSD, content_type="application/xml")
    fake_web.add(EUROSTAT.url(entry.flow, "M.PER..T.ASY_APP+NASY_APP.TOTAL.DE"),
                 "DATAFLOW,LAST UPDATE,freq,unit,citizen,sex,applicant,age,geo,TIME_PERIOD,OBS_VALUE\n"
                 "X,,M,PER,EXT_EU27_2020,T,ASY_APP,TOTAL,DE,2026-08,9000\nX,,M,PER,SY,T,ASY_APP,TOTAL,DE,2026-08,900\n",
                 content_type="text/csv")
    check = check_entry(fake_web.client(), entry, active_regions(path=REGIONS_FIXTURE), TODAY)
    assert check.status == "failed" and "HTTP 400" in check.problems[0]
    assert "citizen: requested TOTAL; missing TOTAL; available EXT_EU27_2020, SY" in check.probe
    assert any(line.startswith("unit: no data with the other parts as requested") for line in check.probe)


def test_eurostat_dimensions_are_read_in_key_order(fake_web) -> None:
    fake_web.add(EUROSTAT_DSD_URL.format(flow="DEMO_R_FIND3"), DSD, content_type="application/xml")
    assert eurostat_dimensions(fake_web.client(), "demo_r_find3") == ["freq", "indic_de", "unit", "geo"]


def test_ecb_entry_with_a_failing_key_is_probed_with_the_template_length(fake_web) -> None:
    entry = entry_of("net_wealth_share")
    fake_web.add(ECB.url(entry.flow, entry.request_key(["DE"])), "KEY,TIME_PERIOD,OBS_VALUE\nX,2026-Q1,1\n",
                 content_type="text/csv")
    check = check_entry(fake_web.client(), entry, active_regions(path=REGIONS_FIXTURE), TODAY)
    assert check.status == "failed" and "dimensions" in check.problems[0]
    assert any(url.startswith(ECB.url("DWA", ".DE.S14._Z._Z.NWA.T10+B50.PT.S.N")) for url in fake_web.requested)
    assert len(check.probe) == 9


def test_unregistered_regions_are_listed(fake_web) -> None:
    catalog = load_catalog(CATALOG_FIXTURE)
    fake_web.add(EUROSTAT.url("demo_r_pjanaggr3", "A.NR.M.TOTAL."),
                 "DATAFLOW,LAST UPDATE,freq,unit,sex,age,geo,TIME_PERIOD,OBS_VALUE\n"
                 "X,,A,NR,M,TOTAL,DE111,2025,1\nX,,A,NR,M,TOTAL,DEG0W,2025,1\nX,,A,NR,M,TOTAL,DEZZZ,2025,1\n"
                 "X,,A,NR,M,TOTAL,FR101,2025,1\n", content_type="text/csv")
    concept, codes = unregistered_regions(fake_web.client(), catalog, {"DE", "DE111"}, TODAY)
    assert (concept, codes) == ("population", ["DEG0W"])


def test_candidates_and_successors_come_from_the_table_of_contents() -> None:
    candidates = candidate_datasets(TOC, {"demo_r_pjanaggr3"}, TODAY)
    assert [item.code for item in candidates] == ["edat_lfse_r3"]
    entry = replace(entry_of("population"), flow="demo_r_pjanaggr9")
    assert possible_successors(entry, TOC)[0].startswith("demo_r_pjanaggr3 (Population on 1 January")


def test_stored_coverage_and_run_failures(tmp_path) -> None:
    catalog = [entry_of("population"), entry_of("total_fertility_rate")]
    regions = active_regions(path=REGIONS_FIXTURE)
    store = SeenStore.load(tmp_path, "sociodemographic_series", "population/DE1")
    store.mark("population.DE111.F", "h", TODAY, data={"values": {"2020": 1.0, "2021": 2.0}})
    store.save()
    unstored, stale = stored_coverage(tmp_path, catalog, regions, TODAY)
    assert unstored == ["total_fertility_rate"]
    assert stale == ["`population`: 1 of 1 series end before the expected period, e.g. `population.DE111.F` (2021)"]

    append_run_log(tmp_path, {"started": "2026-10-30T01:07:00+00:00", "collectors": {"eurostat": {"counters": {
        "not_found_entries": ["median_age"], "failed_entries": ["neet_rate: the response has 6 dimensions"]}}}})
    append_run_log(tmp_path, {"started": "2026-09-01T01:07:00+00:00", "collectors": {"eurostat": {"counters": {
        "not_found_entries": ["old"]}}}})
    failures = recent_run_failures(tmp_path, datetime(2026, 9, 27, tzinfo=timezone.utc))
    assert failures == {"median_age": 1, "neet_rate": 1}


def test_terms_pages_are_compared_by_the_hash_of_their_text(fake_web) -> None:
    provider = load_providers()[0]
    fake_web.add(provider.terms_url, "<html><script>var t = 1;</script><p>Reuse is authorised.</p></html>")
    recorded = check_terms(fake_web.client(), provider)
    assert recorded.status == "recorded" and recorded.new_hash
    same = check_terms(fake_web.client(), replace(provider, terms_hash=recorded.new_hash))
    assert same.status == "unchanged"
    fake_web.add(provider.terms_url, "<html><p>Reuse is not authorised.</p></html>")
    assert check_terms(fake_web.client(), replace(provider, terms_hash=recorded.new_hash)).status == "changed"
    assert check_terms(fake_web.client(), replace(provider, terms_url="https://other.example/terms")).status == \
        "unavailable"
    assert page_text("<p>a <b>b</b></p><style>x{}</style>  c") == "a b c"


def test_run_review_and_report(fake_web, tmp_path) -> None:
    fake_web.add(EUROSTAT_TOC_URL, (FIXTURES_DIR / "sdmx" / "toc.txt").read_text(encoding="utf-8"),
                 content_type="text/plain")
    settings = replace(Settings(), output_dir=tmp_path / "data")
    result = run_review(settings, fake_web.client(), NOW, concepts={"median_age", "unknown_concept"})
    assert result.toc_available
    assert [check.concept for check in result.checks] == ["median_age"]
    assert result.checks[0].status == "no_data"
    assert [(concept, flow) for concept, flow, _successors in result.not_listed] == [("median_age", "demo_r_pjanind3")]
    assert "unknown concepts: unknown_concept" in result.notes
    report = build_report(result, NOW, 54)
    assert report.startswith("# Source review 2026-11")
    assert "| `median_age` | eurostat `demo_r_pjanind3` | no_data |" in report
    assert "## Table of Contents" in report


def test_report_lists_changed_terms() -> None:
    result = ReviewResult(terms=[TermsCheck("ecb", "https://x.example", "changed", "abc", "the text differs")])
    report = build_report(result, NOW, 54)
    assert "- **Terms pages changed or recorded:** 1" in report
    assert "| ecb | https://x.example | changed | the text differs |" in report
    json.dumps(report)
