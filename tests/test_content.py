"""Tests of the page parser."""
from datetime import datetime, timezone

from conftest import FIXTURES_DIR
from content import clean_text, parse_datetime, parse_page


def _article() -> str:
    return (FIXTURES_DIR / "pages" / "news_article.html").read_text(encoding="utf-8")


def test_parse_page_reads_the_jsonld_article() -> None:
    info = parse_page(_article())

    assert info.has_article_data
    assert info.jsonld_type == "NewsArticle"
    assert info.canonical_url == "https://news.example/gesellschaft/geburten-100.html"
    assert info.section == "Gesellschaft"
    assert info.keywords == ["Geburten", "Demografie", "Statistisches Bundesamt"]
    assert info.published_at == datetime(2026, 10, 27, 7, 30, tzinfo=timezone.utc)
    assert info.updated_at == datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    assert info.author_count == 1
    assert info.is_agency_copy is True
    assert info.word_count == 640
    assert info.is_paywalled is True
    assert info.has_video is False
    assert info.tdm_reserved is False


def test_parse_page_takes_the_lead_text_from_the_article_element() -> None:
    info = parse_page(_article())

    assert info.lead_text.startswith("Wiesbaden (dpa) - In Deutschland")
    assert "Kurz." not in info.lead_text
    assert "Navigation" not in info.lead_text


def test_parse_page_without_jsonld_uses_meta_tags() -> None:
    html = ('<html><head><meta name="description" content="Teaser"><meta name="keywords" content="A, B">'
            '<meta name="tdm-reservation" content="1"></head><body><main><p>' + "Wort " * 20 + "</p></main></body></html>")
    info = parse_page(html)

    assert not info.has_article_data
    assert info.description == "Teaser"
    assert info.keywords == ["A", "B"]
    assert info.tdm_reserved is True
    assert info.author_count is None and info.is_paywalled is None
    assert info.lead_text.startswith("Wort Wort")


def test_parse_page_ignores_invalid_jsonld() -> None:
    html = '<html><head><script type="application/ld+json">{invalid</script></head><body></body></html>'
    assert parse_page(html).jsonld_type is None


def test_parse_datetime_handles_iso_rfc2822_and_garbage() -> None:
    assert parse_datetime("2026-10-19T08:30:00Z") == datetime(2026, 10, 19, 8, 30, tzinfo=timezone.utc)
    assert parse_datetime("Mon, 19 Oct 2026 08:30:00 +0200") == datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc)
    assert parse_datetime("2026-10-19") == datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
    assert parse_datetime("yesterday") is None


def test_clean_text_strips_html_and_shortens() -> None:
    assert clean_text("<p>Ein <b>kurzer</b>   Text</p>") == "Ein kurzer Text"
    assert clean_text("x" * 20, limit=10) == "xxxxxxx..."
    assert clean_text(None) == ""
