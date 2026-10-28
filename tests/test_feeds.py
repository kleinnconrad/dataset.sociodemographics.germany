"""Tests of the feeds collector with a fake web and a fake annotator."""
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Optional
from xml.sax.saxutils import escape

import httpx
import pytest

from annotation import AnnotationInput, CallBudgetExceededError, clean_annotation
from collectors.base import RunContext
from collectors.feeds import (
    SOCIAL_TERMS,
    Candidate,
    FeedsCollector,
    interleave,
    looks_sociodemographic,
    parse_feed,
    parse_news_sitemap,
)
from conftest import RUN_STARTED, SOURCES_FIXTURE, read_dataset
from schema import ItemAnnotation
from settings import Settings
from sources import SourceHealthStore, load_config
from state import CollectorEntry
from test_annotation import label

SOURCES = SOURCES_FIXTURE
RECENT = RUN_STARTED - timedelta(hours=5)


def rss(items: list[tuple[str, str, Optional[datetime], str]]) -> str:
    """Builds an RSS feed from (title, link, published, description) tuples."""
    entries = "".join(
        f"<item><title>{escape(title)}</title><link>{escape(link)}</link>"
        + (f"<pubDate>{format_datetime(published)}</pubDate>" if published else "")
        + f"<description>{escape(description)}</description></item>"
        for title, link, published, description in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{entries}</channel></rss>'


def news_sitemap(items: list[tuple[str, str, datetime, str]]) -> str:
    """Builds a Google News sitemap from (title, link, published, keywords) tuples."""
    urls = "".join(
        f"<url><loc>{escape(link)}</loc><news:news><news:publication><news:name>N</news:name>"
        f"<news:language>de</news:language></news:publication>"
        f"<news:publication_date>{published.isoformat()}</news:publication_date>"
        f"<news:title>{escape(title)}</news:title><news:keywords>{escape(keywords)}</news:keywords></news:news></url>"
        for title, link, published, keywords in items
    )
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">' + urls + "</urlset>")


class FakeAnnotator:
    """Annotator double that labels every item and records the inputs."""
    model = "fake-model"
    extraction_hash = "0123456789abcdef"

    def __init__(self, stop_after: Optional[int] = None) -> None:
        self.inputs: list[AnnotationInput] = []
        self.usage = type("Usage", (), {"calls": 0})()
        self._stop_after = stop_after

    def annotate(self, items: list[AnnotationInput]) -> list[Optional[ItemAnnotation]]:
        if self._stop_after is not None and len(self.inputs) >= self._stop_after:
            raise CallBudgetExceededError("MAX_GEMINI_CALLS_PER_RUN (1) reached")
        self.inputs.extend(items)
        return [clean_annotation(ItemAnnotation.model_validate(label(n)), item.kind, item.published)
                for n, item in enumerate(items, start=1)]


def collector(annotator: Any, config_path: Any = SOURCES) -> FeedsCollector:
    return FeedsCollector(annotator, config_path=config_path)


def setup_sources(fake_web: Any) -> None:
    fake_web.add("https://news.example/gesellschaft/index.rss", rss([
        ("Zahl der Geburten sinkt auf Tiefstand", "https://news.example/gesellschaft/geburten-100.html?utm_source=rss",
         RECENT, "So wenige Kinder wie seit Jahrzehnten nicht."),
        ("Alter Artikel", "https://news.example/gesellschaft/alt.html", RUN_STARTED - timedelta(days=10), "alt"),
    ]), content_type="application/rss+xml")
    fake_web.add("https://news.example/sitemap-news.xml", news_sitemap([
        ("Zahl der Geburten sinkt auf Tiefstand", "https://news.example/gesellschaft/geburten-100.html", RECENT,
         "Geburten"),
        ("Bundesliga: Spieltag", "https://news.example/sport/bundesliga.html", RECENT, "Fußball"),
        ("Mieten steigen in Großstädten", "https://news.example/wirtschaft/mieten.html", RECENT, "Wohnen"),
    ]), content_type="application/xml")
    fake_web.add_fixture("https://news.example/gesellschaft/geburten-100.html", "pages/news_article.html")
    fake_web.add("https://news.example/wirtschaft/mieten.html", "<html><body><article><p>"
                 + "Die Mieten in den Großstädten sind im dritten Quartal erneut gestiegen. " * 3
                 + "</p></article></body></html>")
    fake_web.add("https://school.example/feed/", rss([
        ("Lehrkräftemangel an Grundschulen", "https://school.example/news/lehrkraefte", RECENT, ""),
    ]), content_type="application/rss+xml")
    fake_web.add("https://stat.example/presse.rss", rss([
        ("Bevölkerung wächst leicht", "https://stat.example/presse/bevoelkerung", RECENT, "Zahl der Einwohner"),
    ]), content_type="application/rss+xml")
    fake_web.add("https://gov.example/presse.rss", rss([
        ("Kabinett beschließt Straßenbauprogramm", "https://gov.example/presse/strassen", RECENT, "Verkehr"),
        ("Land fördert den Ausbau der Kitas", "https://gov.example/presse/kita", RECENT, "Förderprogramm"),
    ]), content_type="application/rss+xml")


def test_parse_feed_and_sitemap() -> None:
    outlet = load_config(SOURCES).outlets[0]
    feed = parse_feed(rss([("Titel", "https://news.example/a.html", RECENT, "<p>Teaser</p>")]).encode(),
                      outlet, "https://news.example/gesellschaft/index.rss")
    assert feed[0].title == "Titel" and feed[0].teaser == "Teaser" and feed[0].from_topic_feed
    assert feed[0].published_at == RECENT

    sitemap, children = parse_news_sitemap(
        news_sitemap([("T", "https://news.example/x.html", RECENT, "Asyl, Migration")]).encode(), outlet,
        "https://news.example/sitemap-news.xml")
    assert children == [] and sitemap[0].keywords == ["Asyl", "Migration"] and sitemap[0].channel == "news_sitemap"
    assert not sitemap[0].from_topic_feed


def test_sitemap_with_entity_declarations_is_rejected() -> None:
    outlet = load_config(SOURCES).outlets[0]
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>&lol;</loc></url></urlset>')
    with pytest.raises(ValueError):
        parse_news_sitemap(bomb.encode(), outlet, "https://news.example/sitemap-news.xml")


def test_central_european_zone_abbreviations_are_not_read_as_utc() -> None:
    outlet = load_config(SOURCES).outlets[0]
    feed = ('<rss version="2.0"><channel><item><title>T</title><link>https://news.example/gesellschaft/a</link>'
            "<pubDate>Mon, 26 Oct 2026 10:00:00 CET</pubDate></item></channel></rss>")

    candidate = parse_feed(feed.encode(), outlet, "https://news.example/gesellschaft/index.rss")[0]

    assert candidate.published_at == datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)


def test_looks_sociodemographic_filters_only_sources_with_the_topic_filter() -> None:
    config = load_config(SOURCES)
    outlet, school = config.outlets
    office, government = config.press_sources

    def candidate(source: Any, url: str, title: str) -> Candidate:
        return Candidate(source=source, url=url, key=url, title=title, teaser="", published_at=None, channel="rss")

    assert looks_sociodemographic(candidate(outlet, "https://news.example/gesellschaft/x.html", "Irgendwas"))
    assert looks_sociodemographic(candidate(outlet, "https://news.example/panorama/x.html", "Asylanträge gehen zurück"))
    assert not looks_sociodemographic(candidate(outlet, "https://news.example/sport/x.html", "Bayern gewinnt"))
    assert looks_sociodemographic(candidate(school, "https://school.example/news/x", "Neue Lehrpläne"))
    assert looks_sociodemographic(candidate(office, "https://stat.example/presse/x", "Tag der offenen Tür"))
    assert not looks_sociodemographic(candidate(government, "https://gov.example/presse/x", "Neue Autobahn"))
    assert looks_sociodemographic(candidate(government, "https://gov.example/presse/x", "Mehr Geld für Schulen"))


@pytest.mark.parametrize(("title", "expected"), [
    ("Geburtenrate sinkt auf Tiefstand", True), ("Geburtstag der Königin", False),
    ("Tausende Menschen sterben im Krieg", False), ("Sterbefälle über dem Durchschnitt", True),
    ("Bundestag beschließt den Haushalt", False), ("Zahl der Haushalte steigt", True),
    ("Kinder bei Unfall verletzt", False), ("Kinderarmut nimmt zu", True), ("Islamisten planten Anschlag", False),
    ("Islamische Gemeinden fordern Anerkennung", True), ("Das ist eher unwahrscheinlich", False),
    ("Zehn Jahre Ehe für alle", True), ("Regierungsbildung in Thüringen", False), ("Bildungsministerin", True),
])
def test_social_terms_exclude_frequent_other_words(title: str, expected: bool) -> None:
    assert bool(SOCIAL_TERMS.search(title)) is expected


def test_interleave_takes_turns() -> None:
    assert list(interleave([["a1", "a2", "a3"], ["b1"], ["c1", "c2"]])) == ["a1", "b1", "c1", "a2", "c2", "a3"]


def test_collect_writes_items_and_annotations(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    ctx = make_ctx()
    annotator = FakeAnnotator()

    result = collector(annotator).collect(ctx, CollectorEntry(first_run=ctx.today))

    news = read_dataset(ctx.settings.output_dir, "news_items")
    press = read_dataset(ctx.settings.output_dir, "press_releases")
    annotations = read_dataset(ctx.settings.output_dir, "text_annotations")
    assert sorted(item["title"] for item in news) == ["Lehrkräftemangel an Grundschulen",
                                                      "Mieten steigen in Großstädten",
                                                      "Zahl der Geburten sinkt auf Tiefstand"]
    assert sorted(item["title"] for item in press) == ["Bevölkerung wächst leicht", "Land fördert den Ausbau der Kitas"]
    office = next(item for item in press if item["press_source_id"] == "stat_office")
    assert office["actor_type"] == "statistical_office" and office["jurisdiction"] == "DE-BY"
    assert len(annotations) == 5
    births = next(item for item in news if item["title"].startswith("Zahl der Geburten"))
    assert births["channel"] == "rss" and births["content_basis"] == "lead_text"
    assert births["jsonld_type"] == "NewsArticle" and births["is_paywalled"] is True
    assert births["outlet_topic"] is None and births["section"] == "Gesellschaft"
    school = next(item for item in news if item["outlet_id"] == "school_example")
    assert school["outlet_topic"] == "Education and schools" and school["content_basis"] == "title_only"
    annotation = next(a for a in annotations if a["item_id"] == births["record_id"])
    assert annotation["is_sociodemographic"] is True and annotation["topic"] == "Births and fertility"
    assert annotation["population_groups"] == ["Families", "Women"]
    assert annotation["group_tone"] == {"Families": "Neutral", "Women": "Neutral"}
    assert annotation["indicators"] == {"Births": "Falling", "Fertility rate": "Falling"}
    assert annotation["reports_statistics"] is True and annotation["mentions_demographic_change"] is True
    assert annotation["license"].startswith("Derived data of this project")
    assert result.counters["annotated"] == 5 and result.error is None
    rents = next(item for item in news if item["title"].startswith("Mieten"))
    assert rents["channel"] == "news_sitemap" and rents["keywords"] == ["Wohnen"]
    press_input = next(item for item in annotator.inputs if item.source.startswith("Statistisches Landesamt"))
    assert press_input.kind == "press release" and press_input.source.endswith("(statistical office)")


def test_without_annotator_all_items_are_stored_without_labels_and_pages(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    ctx = make_ctx()

    result = collector(None).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert len(read_dataset(ctx.settings.output_dir, "news_items")) == 3
    assert len(read_dataset(ctx.settings.output_dir, "press_releases")) == 2
    assert read_dataset(ctx.settings.output_dir, "text_annotations") == []
    assert result.counters["beyond_cap"] == 5 and result.counters["annotation"].startswith("off")
    assert "https://news.example/gesellschaft/geburten-100.html" not in fake_web.requested
    assert result.error is None and result.complete


def test_second_run_skips_known_items(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    ctx = make_ctx()
    collector(FakeAnnotator()).collect(ctx, CollectorEntry(first_run=ctx.today))
    annotator = FakeAnnotator()

    result = collector(annotator).collect(make_ctx(), CollectorEntry(first_run=ctx.today))

    assert annotator.inputs == []
    assert result.counters["items_selected"] == 0


def test_blocked_source_is_marked_and_skipped(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    fake_web.add("https://stat.example/presse.rss", "Forbidden", status=403)
    ctx = make_ctx()

    collector(FakeAnnotator()).collect(ctx, CollectorEntry(first_run=ctx.today))

    health = SourceHealthStore.load(ctx.settings.state_dir)
    assert health.get("stat_office").status == "blocked"
    assert not health.is_due("stat_office", ctx.today + timedelta(days=30))
    assert all(item["press_source_id"] != "stat_office"
               for item in read_dataset(ctx.settings.output_dir, "press_releases"))


def test_tdm_reservation_excludes_the_source(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    fake_web.add("https://news.example/.well-known/tdmrep.json", [{"location": "/", "tdm-reservation": 1}])
    ctx = make_ctx()

    collector(FakeAnnotator()).collect(ctx, CollectorEntry(first_run=ctx.today))

    health = SourceHealthStore.load(ctx.settings.state_dir)
    assert health.get("news_example").status == "tdm_reserved"
    news = read_dataset(ctx.settings.output_dir, "news_items")
    assert all(item["outlet_id"] != "news_example" for item in news)


def test_budget_stop_writes_items_without_annotations(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    ctx = make_ctx()

    result = collector(FakeAnnotator(stop_after=0)).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert len(read_dataset(ctx.settings.output_dir, "news_items")) == 3
    assert read_dataset(ctx.settings.output_dir, "text_annotations") == []
    assert result.counters["not_annotated"] == 5
    assert result.error is None


def test_cap_annotates_press_releases_first_and_stores_the_rest_without_labels(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    ctx = make_ctx(max_annotations_per_run=3)
    annotator = FakeAnnotator()

    result = collector(annotator).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert [item.kind for item in annotator.inputs].count("press release") == 2
    assert len(annotator.inputs) == 3
    assert len(read_dataset(ctx.settings.output_dir, "news_items")) == 3
    assert len(read_dataset(ctx.settings.output_dir, "text_annotations")) == 3
    assert result.counters["items_selected"] == 3 and result.counters["beyond_cap"] == 2


def test_items_after_the_run_deadline_are_stored_without_labels(fake_web, tmp_path, monkeypatch) -> None:
    setup_sources(fake_web)
    now = [RUN_STARTED]
    serve = fake_web.handler

    def slow_page(request: httpx.Request) -> httpx.Response:
        # The first article page takes longer than the run deadline
        if request.url.path == "/gesellschaft/geburten-100.html":
            now[0] += timedelta(minutes=30)
        return serve(request)

    monkeypatch.setattr(fake_web, "handler", slow_page)
    settings = replace(Settings(), output_dir=tmp_path / "data")
    ctx = RunContext(settings, fake_web.client(), RUN_STARTED, clock=lambda: now[0])
    annotator = FakeAnnotator()

    result = collector(annotator).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not result.complete
    assert result.counters["annotated"] + result.counters["not_annotated"] == 5
    assert len(read_dataset(ctx.settings.output_dir, "news_items")) == 3
    assert len(read_dataset(ctx.settings.output_dir, "press_releases")) == 2
    assert "https://news.example/wirtschaft/mieten.html" not in fake_web.requested


class ParallelAnnotator(FakeAnnotator):
    """Annotator double whose first two calls wait for each other, so that they must run at the same time."""

    def __init__(self) -> None:
        super().__init__()
        self._barrier = threading.Barrier(2, timeout=5)
        self._lock = threading.Lock()
        self._calls = 0

    def annotate(self, items: list[AnnotationInput]) -> list[Optional[ItemAnnotation]]:
        with self._lock:
            self._calls += 1
            call = self._calls
        if call <= 2:
            self._barrier.wait()
        return super().annotate(items)


def test_batches_are_annotated_in_parallel_and_written_in_order(fake_web, make_ctx) -> None:
    titles = [f"Bevölkerungsstand am Stichtag {n}" for n in range(25)]
    fake_web.add("https://stat.example/presse.rss", rss([
        (title, f"https://stat.example/presse/{n}", RECENT - timedelta(minutes=n), "Text")
        for n, title in enumerate(titles)
    ]), content_type="application/rss+xml")
    ctx = make_ctx(gemini_concurrency=3)

    result = collector(ParallelAnnotator()).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters["annotated"] == 25
    assert [item["title"] for item in read_dataset(ctx.settings.output_dir, "press_releases")] == titles
    assert len(read_dataset(ctx.settings.output_dir, "text_annotations")) == 25


def test_removed_sources_are_not_read(fake_web, make_ctx, tmp_path) -> None:
    setup_sources(fake_web)
    config = SOURCES.read_text(encoding="utf-8").replace(
        'feeds = ["https://stat.example/presse.rss"]', 'feeds = ["https://stat.example/presse.rss"]\nremoved = 2026-10-29')
    config_path = tmp_path / "sources.toml"
    config_path.write_text(config, encoding="utf-8")
    ctx = make_ctx()

    collector(FakeAnnotator(), config_path).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert "https://stat.example/presse.rss" not in fake_web.requested
    assert all(item["press_source_id"] != "stat_office"
               for item in read_dataset(ctx.settings.output_dir, "press_releases"))


def test_dry_run_reads_sources_without_writing(fake_web, make_ctx) -> None:
    setup_sources(fake_web)
    ctx = make_ctx(dry_run=True)

    result = collector(FakeAnnotator()).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters["items_selected"] == 5
    assert not (ctx.settings.output_dir / "news_items").exists()
    assert "https://news.example/gesellschaft/geburten-100.html" not in fake_web.requested
