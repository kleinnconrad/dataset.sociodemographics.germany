"""Tests of the monthly source review with a fake web."""
import json
from dataclasses import replace
from datetime import date, datetime, timezone

from catalog import load_catalog
from collectors.sdmx import ECB, EUROSTAT, EUROSTAT_TOC_URL, parse_toc
from conftest import CATALOG_FIXTURE, FIXTURES_DIR, PROVIDERS_FIXTURE, REGIONS_FIXTURE
from providers import load_providers
from regions import active_regions
from settings import Settings
from snapshots import ExcludedSource, load_snapshot_config
from source_review import (
    EUROSTAT_DSD_URL,
    ReviewResult,
    SnapshotCheck,
    TermsCheck,
    build_report,
    candidate_datasets,
    check_entry,
    check_snapshot,
    check_terms,
    eurostat_dimensions,
    first_row_labels,
    genesis_candidates,
    is_stale,
    page_text,
    period_end,
    possible_successors,
    recent_run_failures,
    run_review,
    stored_coverage,
    unregistered_regions,
)
from state import SeenEntry, SeenStore, append_run_log

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
    result = run_review(settings, fake_web.client(), NOW, concepts={"median_age", "unknown_concept"},
                        snapshot_ids=set())
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


SNAPSHOT_FIXTURE = FIXTURES_DIR / "config" / "snapshots.toml"
PROVIDER_IDS = {provider.id for provider in load_providers(PROVIDERS_FIXTURE)}
ZENSUS = "https://ergebnisse.zensus2022.de/api/rest/2020/"
PAGE = "https://wahl.example/strukturdaten.html"
FILE = "https://wahl.example/dam/jcr/abc/btw2025_strukturdaten.csv"
STRUCTURE_CSV = "Strukturdaten\n© Die Bundeswahlleiterin\nWkr-Nr.;Name;Bevölkerung\n1;Flensburg – Schleswig;250,3\n"


def snapshot_sources():
    return {source.id: source for source in load_snapshot_config(SNAPSHOT_FIXTURE, PROVIDER_IDS)[0]}


def stored_entry(url: str, changed: str, item_hash: str = "old") -> SeenEntry:
    return SeenEntry(hash=item_hash, first_seen=date(2026, 1, 6), last_seen=date(2026, 10, 30), revision=0,
                     data={"url": url, "changed": changed})


def test_snapshot_check_reports_changed_addresses_and_old_content(fake_web) -> None:
    fake_web.add(PAGE, '<a href="/dam/jcr/abc/btw2025_strukturdaten.csv">CSV</a>')
    fake_web.add(FILE, STRUCTURE_CSV, content_type="text/csv")
    source = replace(snapshot_sources()["bundeswahlleiterin.btw2025_strukturdaten"], update_days=200)
    check = check_snapshot(fake_web.client(), source, stored_entry(FILE, "2026-09-01"), TODAY)
    assert check.status == "ok" and check.rows == 1 and check.title == "btw2025_strukturdaten.csv"
    assert check.columns == ["Wkr-Nr.", "Name", "Bevölkerung"] and check.first_row == ["1", "Flensburg – Schleswig", "250,3"]
    assert check.notes == ["the content changed since the last observation; the next run stores it"]
    moved = check_snapshot(fake_web.client(), source, stored_entry("https://wahl.example/old.csv", "2026-01-02"), TODAY)
    assert moved.status == "attention"
    assert any("file address changed" in problem for problem in moved.problems)
    assert any("did not change since 2026-01-02" in problem for problem in moved.problems)
    assert check_snapshot(fake_web.client(), source, None, TODAY).notes == ["not stored yet"]
    never = check_snapshot(fake_web.client(), replace(source, added=date(2026, 10, 1)), None, TODAY)
    assert never.status == "attention" and "never stored" in never.problems[0]
    wrong = check_snapshot(fake_web.client(), replace(source, expect="Wahlkreis-Nr"), None, TODAY)
    assert wrong.status == "failed" and "does not match" in wrong.problems[0]


def test_snapshot_check_without_token_reports_the_secret(fake_web, monkeypatch) -> None:
    monkeypatch.delenv("ZENSUS2022_TOKEN", raising=False)
    fake_web.add(ZENSUS + "metadata/table", {"Status": {"Code": 15, "Content": "Sie sind nicht berechtigt."}})
    check = check_snapshot(fake_web.client(), snapshot_sources()["zensus2022.1000A-1018"], None, TODAY)
    assert check.status == "no_access" and "ZENSUS2022_TOKEN" in check.problems[0]
    missing = check_snapshot(fake_web.client(), snapshot_sources()["ekd.kirchenmitglieder_bericht"], None, TODAY)
    assert missing.status == "failed" and "404" in missing.problems[0]


def test_genesis_candidates_need_a_token(fake_web, monkeypatch) -> None:
    sources = list(snapshot_sources().values())
    monkeypatch.delenv("ZENSUS2022_TOKEN", raising=False)
    assert genesis_candidates(fake_web.client(), sources) == (
        [], ["zensus2022: no candidate tables listed; the secret ZENSUS2022_TOKEN is not set"])
    monkeypatch.setenv("ZENSUS2022_TOKEN", "token")
    fake_web.add(ZENSUS + "catalogue/tables", {"Status": {"Code": 0}, "List": [
        {"Code": "1000A-1018", "Content": "Personen: Religion"}, {"Code": "1000A-1019", "Content": "Personen:\nAlter"}]})
    assert genesis_candidates(fake_web.client(), sources) == ([("zensus2022", "1000A-1019", "Personen: Alter")], [])


def test_review_of_snapshots_only_skips_the_catalog(fake_web, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ZENSUS2022_TOKEN", raising=False)
    fake_web.add(PAGE, '<a href="/dam/jcr/abc/btw2025_strukturdaten.csv">CSV</a>')
    fake_web.add(FILE, STRUCTURE_CSV, content_type="text/csv")
    settings = replace(Settings(), output_dir=tmp_path / "data")
    result = run_review(settings, fake_web.client(), NOW, concepts=set(),
                        snapshot_ids={"bundeswahlleiterin.btw2025_strukturdaten", "unknown.entry"},
                        snapshots_path=SNAPSHOT_FIXTURE, provider_ids=PROVIDER_IDS)
    assert not result.checks and not result.toc_available
    assert [check.snapshot_id for check in result.snapshots] == ["bundeswahlleiterin.btw2025_strukturdaten"]
    assert "unknown snapshot entries: unknown.entry" in result.notes
    assert [item.id for item in result.excluded] == ["dbk.kirchliche_statistik"]
    assert not any(EUROSTAT_TOC_URL in url for url in fake_web.requested)


def test_report_lists_snapshot_problems_and_excluded_sources() -> None:
    result = ReviewResult(
        snapshots=[SnapshotCheck("zensus2022.1000A-1018", "genesis", "no_access",
                                 problems=["no access without an account; set the secret ZENSUS2022_TOKEN"]),
                   SnapshotCheck("ekd.bericht", "xlsx", rows=12, title="Bericht.xlsx (Tabelle 1)", changed="2026-10-02",
                                 columns=[f"c{number}" for number in range(10)], first_row=["01", None])],
        genesis_candidates=[("zensus2022", "1000A-1019", "Personen: Alter")],
        excluded=[ExcludedSource("dbk.x", "DBK", "https://www.dbk.de/", date(2025, 6, 1), "HTTP 401")])
    report = build_report(result, NOW, 54)
    assert "- [Snapshot sources](#snapshot-sources)" in report and "## Snapshot sources" in report
    assert "- **Snapshot entries:** 1 ok, 0 need attention, 1 without access, 0 failed" in report
    assert "- **Excluded sources due for a new check:** 1" in report
    assert "| `zensus2022.1000A-1018` | genesis | no_access | no access without an account" in report
    assert "| zensus2022 | `1000A-1019` | Personen: Alter |" in report
    assert "| c0 ; c1 ; c2 ; c3 ; c4 ; c5 ; c6 ; c7 ; ... (10 cells) | 01 ; |" in report
    assert "| DBK (`dbk.x`) | 2025-06-01 (due) | HTTP 401 |" in report


def test_first_row_labels_name_the_codes_of_a_genesis_table() -> None:
    columns = ["time_code", "time", "1_variable_code", "1_variable_attribute_code", "2_variable_code",
               "2_variable_attribute_code", "value"]
    row = ["STAG", "2022-05-15", "GEOBL1", "05", "RELZG2", None, "100,0"]
    labels = {"GEOBL1": "Bundesländer", "GEOBL1:05": "Nordrhein-Westfalen", "RELZG2": "Religion"}
    assert first_row_labels(columns, row, labels) == ["Bundesländer: Nordrhein-Westfalen", "Religion: total"]
    assert first_row_labels(["Wkr-Nr.", "Name"], ["1", "Flensburg"], {}) == []
