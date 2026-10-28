"""Tests of the review of the news and press sources with a fake web and a fake search model."""
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import press_review
import source_review
from conftest import FIXTURES_DIR, PROVIDERS_FIXTURE, FakeWeb
from press_review import (
    Change,
    NewEntry,
    SearchError,
    SearchModel,
    apply_changes,
    build_press_section,
    check_feed,
    count_items,
    feed_links,
    find_gaps,
    parse_json_array,
    press_summary,
    run_press_review,
    toml_value,
)
from settings import Settings
from sources import SourceHealth, SourceHealthStore, load_config
from storage import dumps_records, partition_file

NOW = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
RECENT = NOW - timedelta(days=1)

CONFIG = """\
# Test sources of the press review.

# feeds: 10 h
[[outlet]]
id = "news_example"
name = "News Example"
homepage = "https://news.example/"
outlet_type = "national_daily"
owner_group = "Example Group"
paywall_model = "mixed"
feeds = ["https://news.example/gesellschaft/index.rss", "https://news.example/old.rss", "https://news.example/gone.rss"]
news_sitemap = "https://news.example/sitemap-news.xml"
fetch_pages = true
added = 2026-01-05

[[outlet]]
id = "daily_example"
name = "Daily Example"
homepage = "https://daily.example/"
outlet_type = "regional_daily"
owner_group = "Example Verlag"
home_state = "DE-SN"
paywall_model = "free"
feeds = ["https://daily.example/rss"]
fetch_pages = true
added = 2026-01-05

[[outlet]]
id = "quiet_example"
name = "Quiet Example"
homepage = "https://quiet.example/"
outlet_type = "regional_daily"
owner_group = "Quiet Verlag"
home_state = "DE-TH"
paywall_model = "free"
feeds = ["https://quiet.example/rss"]
added = 2026-01-05

[[outlet]]
id = "kept_example"
name = "Kept Example"
homepage = "https://kept.example/"
outlet_type = "specialist_press"
owner_group = "Kept Media"
topic = "Migration and asylum"
paywall_model = "free"
feeds = ["https://kept.example/rss"]
added = 2026-01-05
keep = true

[[press_source]]
id = "office_example"
name = "Landesamt Beispiel"
homepage = "https://office.example/"
actor_type = "statistical_office"
jurisdiction = "DE-BY"
feeds = ["https://office.example/presse.rss"]
fetch_pages = false
added = 2026-01-05

[[press_source]]
id = "blocked_example"
name = "Blocked Ministry"
homepage = "https://blocked.example/"
actor_type = "federal_ministry"
jurisdiction = "DE"
feeds = ["https://blocked.example/rss"]
fetch_pages = true
added = 2026-01-05

# Candidates that were checked and not admitted.
[[excluded]]
id = "excluded_example"
name = "Excluded Example"
homepage = "https://excluded.example/"
status = "blocked"
reason = "blocks automated requests"
checked = 2026-01-05
"""


def rss(items: list[tuple[str, str, datetime]]) -> str:
    """Builds an RSS feed from (title, link, published) tuples."""
    entries = "".join(f"<item><title>{title}</title><link>{link}</link><pubDate>{format_datetime(published)}"
                      f"</pubDate></item>" for title, link, published in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{entries}</channel></rss>'


def social_feed(host: str, count: int = 6, published: datetime = RECENT) -> str:
    return rss([(f"Asylanträge steigen im Monat {n}", f"https://{host}/gesellschaft/artikel-{n}.html", published)
                for n in range(count)])


def news_sitemap(host: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url>'
            f"<loc>https://{host}/gesellschaft/a.html</loc><news:news><news:publication><news:name>N</news:name>"
            f"<news:language>de</news:language></news:publication><news:publication_date>{RECENT.isoformat()}"
            "</news:publication_date><news:title>Geburtenrate sinkt</news:title></news:news></url></urlset>")


def serve_sources(fake_web: FakeWeb) -> None:
    """Serves the feeds of the configured test sources."""
    fake_web.add("https://news.example/robots.txt",
                 "# Die Nutzung für Text- und Data-Mining (§ 44b UrhG) ist vorbehalten.\nUser-agent: *\nAllow: /\n",
                 content_type="text/plain")
    fake_web.add("https://news.example/gesellschaft/index.rss", social_feed("news.example"),
                 content_type="application/rss+xml")
    fake_web.add("https://news.example/old.rss", social_feed("news.example", 2, NOW - timedelta(days=400)),
                 content_type="application/rss+xml")
    fake_web.add("https://news.example/sitemap-news.xml", news_sitemap("news.example"), content_type="application/xml")
    fake_web.add("https://daily.example/robots.txt", "User-agent: *\nCrawl-delay: 30\n", content_type="text/plain")
    fake_web.add("https://daily.example/rss", "", status=301, headers={"location": "https://daily.example/feed.xml"})
    fake_web.add("https://daily.example/feed.xml", social_feed("daily.example"), content_type="application/rss+xml")
    fake_web.add("https://kept.example/rss", social_feed("kept.example"), content_type="application/rss+xml")
    fake_web.add("https://office.example/presse.rss", social_feed("office.example", 2),
                 content_type="application/rss+xml")


def serve_candidates(fake_web: FakeWeb) -> None:
    """Serves the websites of the candidates that the fake search returns."""
    fake_web.add("https://ministry.example/", "<html><body>Ministerium</body></html>")
    fake_web.add("https://ministry.example/presse.rss", social_feed("ministry.example", 3),
                 content_type="application/rss+xml")
    fake_web.add("https://agency.example/", "Forbidden", status=403)
    fake_web.add("https://institut.example/", '<html><head><link rel="alternate" type="application/rss+xml" '
                 'href="/presse/feed.xml"></head><body></body></html>')
    fake_web.add("https://institut.example/presse/feed.xml", social_feed("institut.example", 4),
                 content_type="application/rss+xml")
    fake_web.add("https://landesamt.example/", "<html><body><a href='/service/rss-feeds'>RSS</a></body></html>")
    fake_web.add("https://landesamt.example/service/rss-feeds",
                 "<html><body><a href='/presse/pressemitteilungen.rss'>Pressemitteilungen</a></body></html>")
    fake_web.add("https://landesamt.example/presse/pressemitteilungen.rss", social_feed("landesamt.example", 2),
                 content_type="application/rss+xml")
    fake_web.add("https://zeitung.example/", "<html><body>Zeitung</body></html>")
    fake_web.add("https://zeitung.example/feed.rss", social_feed("zeitung.example", 8),
                 content_type="application/rss+xml")
    fake_web.add("https://fachblatt.example/", "<html><body>Fachblatt</body></html>")
    fake_web.add("https://fachblatt.example/robots.txt", "# Kein Text und Data Mining\nUser-agent: *\nAllow: /\n",
                 content_type="text/plain")
    fake_web.add("https://fachblatt.example/feed/", rss([(f"Neue Lehrpläne {n}", f"https://fachblatt.example/n{n}",
                                                          RECENT) for n in range(6)]),
                 content_type="application/rss+xml")
    fake_web.add("https://reserved.example/.well-known/tdmrep.json", [{"location": "/", "tdm-reservation": 1}])


class FakeSearch:
    """Search double that answers each category with a prepared JSON text."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.prompts: list[str] = []
        self.calls = 0

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.calls += 1
        for marker, answer in self.answers.items():
            if marker in prompt:
                return answer if isinstance(answer, str) else json.dumps(answer)
        return "[]"


ANSWERS = {
    "federal institutions for population": [
        {"name": "Bundesministerium für Beispiele", "homepage": "https://ministry.example/",
         "feeds": ["https://ministry.example/presse.rss"], "actor_type": "federal_ministry"},
        {"name": "Amt für Sperren", "homepage": "https://agency.example/", "feeds": [], "actor_type": "public_agency"},
        {"name": "Ausgeschlossen", "homepage": "https://excluded.example/", "feeds": [], "actor_type": "public_agency"},
    ],
    "research institutes on demography": "```json\n" + json.dumps([
        {"name": "Institut für Bevölkerungsforschung", "homepage": "https://institut.example/", "feeds": [],
         "actor_type": "research_institute"},
        {"name": "Sportverein", "homepage": "https://sport.example/", "feeds": [], "actor_type": "club"},
    ]) + "\n```",
    "Statistische Landesämter": [
        {"name": "Hessisches Landesamt", "homepage": "https://landesamt.example/", "feeds": [], "jurisdiction": "DE-HE"},
        {"name": "Bayerisches Landesamt", "homepage": "https://bayern.example/", "feeds": [], "jurisdiction": "DE-BY"},
        {"name": "Landesamt Irgendwo", "homepage": "https://irgendwo.example/", "feeds": [], "jurisdiction": "Bavaria"},
    ],
    "regional daily newspapers": [
        {"name": "Thüringer Zeitung", "homepage": "https://zeitung.example/", "feeds": ["https://zeitung.example/feed.rss"],
         "home_state": "DE-TH", "owner_group": "Zeitungsverlag", "paywall_model": "mixed"},
    ],
    "specialist publications": [
        {"name": "Fachblatt Schule", "homepage": "https://fachblatt.example/", "feeds": ["https://fachblatt.example/feed/"],
         "topic": "Education and schools", "owner_group": "Fachverlag", "paywall_model": "free"},
        {"name": "Vorbehalt-Kurier", "homepage": "https://reserved.example/", "feeds": [],
         "topic": "Religion and churches", "owner_group": "Vorbehalt Verlag", "paywall_model": "hard"},
    ],
}


@pytest.fixture
def review_setup(tmp_path: Path, fake_web: FakeWeb) -> tuple[Settings, Path]:
    """Writes the test configuration, the state and recent records of three sources."""
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    settings = Settings(output_dir=tmp_path / "data")
    health = SourceHealthStore(settings.state_dir / "sources.json", {
        "blocked_example": SourceHealth(status="blocked", last_attempt=NOW.date() - timedelta(days=10),
                                        last_error="blocked (HTTP 403): https://blocked.example/rss"),
    })
    health.save()
    for dataset, source_id in (("news_items", "news_example"), ("news_items", "daily_example"),
                               ("press_releases", "office_example")):
        path = partition_file(settings.output_dir, dataset, (NOW - timedelta(days=10)).date())
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        path.write_text(dumps_records([*existing, {"record_id": f"x:{source_id}", "source_id": source_id}]),
                        encoding="utf-8")
    serve_sources(fake_web)
    serve_candidates(fake_web)
    return settings, config_path


def changes_by(result: Any) -> dict[tuple[str, str], Change]:
    return {(change.action, change.source_id): change for change in result.changes
            if change.action not in ("remove_feed",)}


def test_review_proposes_removals_fixes_additions_and_exclusions(review_setup, fake_web) -> None:
    settings, config_path = review_setup
    search = FakeSearch(ANSWERS)

    text, result = run_press_review(settings, config_path, fake_web.client(), search, NOW)

    found = changes_by(result)
    assert "no sociodemographic items" in found[("remove_source", "quiet_example")].reason
    assert found[("remove_source", "blocked_example")].reason.startswith("blocked since")
    assert ("remove_source", "kept_example") not in found
    removed_feeds = {change.old: change.reason for change in result.changes if change.action == "remove_feed"}
    assert removed_feeds["https://news.example/old.rss"].startswith("newest item from")
    assert "404" in removed_feeds["https://news.example/gone.rss"]
    assert "natural language" in found[("set_fetch_pages", "news_example")].reason
    assert found[("replace_feed", "daily_example")].new == "https://daily.example/feed.xml"
    assert "crawl delay 30 s" in found[("set_fetch_pages", "daily_example")].reason
    assert not any("blocked.example" in url or "quiet.example" in url for url in fake_web.requested)
    assert not any("excluded.example" in url for url in fake_web.requested)

    # Bayern has its office; Brandenburg and Schleswig-Holstein count as covered only with Berlin and Hamburg
    assert "DE-BY" not in result.gaps.statistical_states and {"DE-BB", "DE-SH"} <= set(result.gaps.statistical_states)
    assert result.gaps.regional_states == tuple(state for state in press_review.STATES if state != "DE-SN")
    assert "Migration and asylum" not in result.gaps.topics and result.gaps.topics[0] == "Education and schools"
    assert result.searches == ["federal", "associations", "statistical_offices", "regional_press", "specialist_press"]

    added = {change.source_id: change.entry for change in result.changes if change.action == "add_source"}
    assert set(added) == {"ministry", "institut", "landesamt", "zeitung", "fachblatt"}
    assert added["institut"].fields["feeds"] == ["https://institut.example/presse/feed.xml"]
    assert added["landesamt"].fields["feeds"] == ["https://landesamt.example/presse/pressemitteilungen.rss"]
    assert added["landesamt"].fields["jurisdiction"] == "DE-HE"
    assert added["landesamt"].fields["actor_type"] == "statistical_office"
    assert added["zeitung"].fields["home_state"] == "DE-TH" and added["zeitung"].fields["outlet_type"] == "regional_daily"
    assert added["fachblatt"].fields["fetch_pages"] is False
    assert added["fachblatt"].fields["topic"] == "Education and schools"
    assert added["ministry"].fields["fetch_pages"] is True and added["ministry"].fields["topic_filter"] is None
    excluded = {change.entry.fields["homepage"]: change.entry.fields for change in result.changes
                if change.action == "exclude"}
    assert excluded["https://agency.example/"]["status"] == "blocked"
    assert excluded["https://reserved.example/"]["status"] == "tdm_reserved"
    assert excluded["https://reserved.example/"]["topic"] == "Religion and churches"
    # A candidate for a gap that a listed source fills is not checked; one outside the searched gaps is
    # rejected without an exclusion
    assert not any("bayern.example" in url for url in fake_web.requested)
    assert any(rejection.homepage == "https://irgendwo.example/" and rejection.status is None
               for rejection in result.rejections)

    config = press_review.validate_config_text(text)
    by_id = {source.id: source for source in config.sources}
    assert by_id["quiet_example"].removed == NOW.date() and not by_id["quiet_example"].active
    assert by_id["news_example"].feeds == ("https://news.example/gesellschaft/index.rss",)
    assert by_id["news_example"].fetch_pages is False and by_id["news_example"].news_sitemap is not None
    assert by_id["daily_example"].feeds == ("https://daily.example/feed.xml",)
    assert by_id["zeitung"].topic_filter and not by_id["fachblatt"].topic_filter
    assert by_id["landesamt"].added == NOW.date()
    assert [entry.id for entry in config.excluded] == ["excluded_example", "agency", "reserved"]
    assert "# source review 2026-11-01: feed https://news.example/old.rss removed" in text

    lines = build_press_section(result, load_config(config_path), NOW, search=True)
    section = "\n".join(lines)
    for heading in ("## News and press sources", "### Removals", "### Feeds and page fetching", "### Additions",
                    "### Exclusions", "### Gaps"):
        assert heading in section
    assert "| Quiet Example (`quiet_example`) | no sociodemographic items in the last 90 days |" in section
    assert "6 items, 6 pass the topic filter" in section
    assert press_summary(result, load_config(config_path)).startswith("- **News and press sources:** 6 of 6 checked; proposed 2 removals")


def test_review_of_selected_sources_skips_gaps_rechecks_and_search(review_setup, fake_web) -> None:
    settings, config_path = review_setup
    search = FakeSearch(ANSWERS)

    _text, result = run_press_review(settings, config_path, fake_web.client(), search, NOW,
                                     source_ids={"daily_example"})

    assert result.selected and result.checked_sources == 1 and search.calls == 0
    assert [source_id for source_id, _summary in result.checks] == ["daily_example"]
    assert not any(change.action in ("add_source", "exclude") for change in result.changes)
    assert "only the selected sources" in "\n".join(build_press_section(result, load_config(config_path), NOW, False))
    with pytest.raises(ValueError, match="unknown_source"):
        run_press_review(settings, config_path, fake_web.client(), None, NOW, source_ids={"unknown_source"})


def test_excluded_candidates_are_checked_again_by_every_review(review_setup, fake_web) -> None:
    settings, config_path = review_setup
    config_path.write_text(CONFIG + (
        "\n[[excluded]]\nid = \"later_institut\"\nname = \"Späteres Institut\"\nhomepage = \"https://later.example/\"\n"
        "kind = \"press_source\"\nactor_type = \"research_institute\"\njurisdiction = \"DE\"\n"
        "feeds = [\"https://later.example/rss\"]\nstatus = \"no_feed\"\nreason = \"no feed\"\nchecked = 2026-10-01\n"),
        encoding="utf-8")
    fake_web.add("https://later.example/", "<html></html>")
    fake_web.add("https://later.example/rss", social_feed("later.example", 2), content_type="application/rss+xml")

    text, result = run_press_review(settings, config_path, fake_web.client(), None, NOW)

    assert result.rechecked == 1
    found = changes_by(result)
    assert ("add_source", "later_institut") in found and ("drop_exclusion", "later_institut") in found
    config = press_review.validate_config_text(text)
    assert "later_institut" in {source.id for source in config.press_sources}
    assert "later_institut" not in {entry.id for entry in config.excluded}


def test_review_without_search_changes_only_existing_sources(review_setup, fake_web) -> None:
    settings, config_path = review_setup

    _text, result = run_press_review(settings, config_path, fake_web.client(), None, NOW)

    assert result.searches == [] and result.candidates == 0
    assert not any(change.action in ("add_source", "exclude") for change in result.changes)
    lines = build_press_section(result, load_config(config_path), NOW, False)
    assert any("search for new sources was turned off" in line for line in lines)


def test_unchanged_review_keeps_the_text(tmp_path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    assert apply_changes(CONFIG, load_config(config_path), [], NOW.date()) == CONFIG


def test_apply_changes_keeps_other_entries_byte_identical(tmp_path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    changes = [Change("remove_source", "quiet_example", 'no "items"')]

    text = apply_changes(CONFIG, load_config(config_path), changes, NOW.date())

    assert 'removed_reason = "no \\"items\\""' in text
    assert text.replace('removed = 2026-11-01\nremoved_reason = "no \\"items\\""\n', "") == CONFIG
    assert press_review.validate_config_text(text).outlets[2].removed_reason == 'no "items"'


def test_dropped_exclusion_is_removed_from_the_configuration(tmp_path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG.replace('status = "blocked"', 'status = "no_feed"'), encoding="utf-8")
    config = load_config(config_path)
    entry = NewEntry("press_source", {"id": "excluded_ministry", "name": "Ministerium", "homepage": "https://x.example/",
                                      "actor_type": "federal_ministry", "jurisdiction": "DE",
                                      "feeds": ["https://x.example/rss"], "fetch_pages": True, "added": NOW.date()})
    changes = [Change("add_source", "excluded_ministry", "admitted", entry=entry),
               Change("drop_exclusion", "excluded_example", "admitted")]

    text = apply_changes(config_path.read_text(encoding="utf-8"), config, changes, NOW.date())

    parsed = press_review.validate_config_text(text)
    assert parsed.excluded == ()
    assert parsed.press_sources[-1].id == "excluded_ministry"


def test_find_gaps_counts_sources_that_leave_as_missing(tmp_path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    config = load_config(config_path)

    assert "DE-BY" not in find_gaps(config).statistical_states
    gaps = find_gaps(config, frozenset({"office_example", "kept_example", "daily_example"}))
    assert "DE-BY" in gaps.statistical_states and "DE-SN" in gaps.regional_states
    assert "Migration and asylum" in gaps.topics and len(gaps.topics) == press_review.MAX_TOPICS_PER_SEARCH


def test_count_items_reads_only_recent_partitions(tmp_path) -> None:
    for day, source_id in ((NOW.date() - timedelta(days=100), "old"), (NOW.date() - timedelta(days=5), "new")):
        path = partition_file(tmp_path, "news_items", day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps_records([{"record_id": "r", "source_id": source_id}]), encoding="utf-8")

    assert count_items(tmp_path, NOW.date() - timedelta(days=90)) == {"new": 1}


def test_check_feed_reports_redirects_and_errors(fake_web) -> None:
    serve_sources(fake_web)
    config = press_review.validate_config_text(CONFIG)
    web = fake_web.client()

    moved = check_feed(web, "https://daily.example/rss", config.outlets[1])
    missing = check_feed(web, "https://news.example/gone.rss", config.outlets[0])

    assert moved.outcome == "ok" and moved.moved_to == "https://daily.example/feed.xml" and len(moved.entries) == 6
    assert missing.outcome == "not_found"


def test_feed_served_as_html_is_read(fake_web) -> None:
    fake_web.add("https://office.example/presse.rss", social_feed("office.example", 3), content_type="text/html")
    config = press_review.validate_config_text(CONFIG)

    check = check_feed(fake_web.client(), "https://office.example/presse.rss", config.press_sources[0])

    assert check.outcome == "ok" and len(check.entries) == 3


def test_feed_links_and_json_answers() -> None:
    html = ('<html><head><link rel="alternate" type="application/rss+xml" href="/rss.xml">'
            '<link rel="stylesheet" href="/a.css"></head><body><a href="/presse/feed/">Feed</a>'
            '<a href="/feedback">Feedback</a></body></html>')
    assert feed_links(html, "https://site.example/start") == ["https://site.example/rss.xml",
                                                              "https://site.example/presse/feed/"]
    assert parse_json_array('Here: ```json\n[{"a": 1}]\n```') == [{"a": 1}]
    with pytest.raises(ValueError):
        parse_json_array("no list")
    assert toml_value(["a", 'b"c']) == '["a", "b\\"c"]'
    assert toml_value(NOW.date()) == "2026-11-01"


def test_search_model_stops_at_the_budget_and_wraps_errors() -> None:
    answers = iter([SimpleNamespace(text="[]", usage_metadata=None)] * 10)
    client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kwargs: next(answers)))
    model = SearchModel(Settings(gemini_api_key="key"), client=client, sleep=lambda seconds: None)

    for _ in range(press_review.MAX_SEARCH_CALLS):
        assert model.ask("prompt") == "[]"
    with pytest.raises(SearchError):
        model.ask("prompt")


def test_section_lists_notes(tmp_path) -> None:
    config_path = tmp_path / "sources.toml"
    config_path.write_text(CONFIG, encoding="utf-8")
    many = press_review.ReviewResult(notes=[f"note {n}" for n in range(press_review.MAX_REPORT_NOTES + 3)])

    lines = build_press_section(many, load_config(config_path), NOW, True)

    assert "- note 0" in lines
    assert "- 3 further notes are in the workflow log" in lines
    assert "- **Topics without a specialist outlet (searched):** none" in lines


def test_source_review_main_reviews_selected_press_sources(review_setup, fake_web, monkeypatch) -> None:
    settings, config_path = review_setup
    monkeypatch.setattr(source_review, "WebClient", fake_web.client)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    report = config_path.parent / "report.md"

    exit_code = source_review.main([
        "--report", str(report), "--sources-config", str(config_path), "--press-sources", "quiet_example,news_example",
        "--providers", str(PROVIDERS_FIXTURE), "--snapshot-config", str(FIXTURES_DIR / "config" / "snapshots.toml"),
        "--output-dir", str(settings.output_dir), "--no-terms"])

    assert exit_code == 0
    text = report.read_text(encoding="utf-8")
    assert "## News and press sources" in text and "- [News and press sources](#news-and-press-sources)" in text
    assert "- **News and press sources:** 2 of 6 checked" in text
    assert "removed = 2026-" in config_path.read_text(encoding="utf-8")


def test_repository_configuration_is_valid() -> None:
    config = load_config()
    assert all(source.added is not None for source in config.sources)
    assert {entry.id for entry in config.excluded} >= {"faz", "zeit", "ekd"}
    assert all(topic in press_review.SPECIALIST_TOPICS or topic is None
               for topic in (source.topic for source in config.outlets))



def test_candidate_check_stops_at_the_time_budget_without_a_status(fake_web) -> None:
    fake_web.add("https://slow.example/", "<html></html>")
    fake_web.add("https://slow.example/rss", social_feed("slow.example", 2), content_type="application/rss+xml")
    candidate = press_review.PressCandidate(name="Langsames Institut", homepage="https://slow.example/",
                                            feeds=["https://slow.example/rss"], actor_type="research_institute")

    entry, rejection = press_review.evaluate_candidate(
        fake_web.client(), "associations", candidate, press_review.Gaps(), NOW, set(), set(), {},
        out_of_time=lambda: True)

    assert entry is None and rejection is not None
    assert rejection.status is None and "time budget" in rejection.reason
    assert "https://slow.example/" in fake_web.requested and "https://slow.example/rss" not in fake_web.requested
