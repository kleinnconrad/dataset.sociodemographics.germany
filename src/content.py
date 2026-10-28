"""
Extracts metadata and lead text from article and press-release pages.

Most German news sites describe their articles with schema.org JSON-LD. The
parser reads the article object (type, section, keywords, dates, authors,
word count, paywall flag, video), the canonical URL and a ``tdm-reservation``
meta tag, and takes the first paragraphs of the article as lead text for the
annotation. The lead text is not stored.
"""
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ARTICLE_TYPES = frozenset({
    "Article", "NewsArticle", "ReportageNewsArticle", "OpinionNewsArticle", "AnalysisNewsArticle",
    "BackgroundNewsArticle", "ReviewNewsArticle", "AskPublicNewsArticle", "LiveBlogPosting", "BlogPosting",
    "Report", "SatiricalArticle", "TechArticle",
})
AGENCY_PATTERN = re.compile(r"(?<![\w-])(dpa|afp|reuters|epd|kna|ap|dts|sid)(?![\w-])", re.IGNORECASE)
# Agency credits in the dateline, e.g. "Berlin (dpa) -"
DATELINE_PATTERN = re.compile(r"^\W*[\w .\-/]{0,40}\((?:dpa|afp|reuters|epd|kna|ap|dts)[^)]{0,20}\)", re.IGNORECASE)
LEAD_TEXT_CHARS = 1500
MIN_PARAGRAPH_CHARS = 40
MAX_TEASER_CHARS = 600


@dataclass
class PageInfo:
    """Metadata and lead text of a page.

    Attributes:
        canonical_url (Optional[str]): Canonical URL declared by the page.
        jsonld_type (Optional[str]): schema.org type of the article object.
        section (Optional[str]): Section from the JSON-LD.
        keywords (list[str]): Keywords from the JSON-LD or the keywords meta tag.
        description (Optional[str]): Teaser from the JSON-LD or the description meta tag.
        published_at (Optional[datetime]): Publication time (UTC).
        updated_at (Optional[datetime]): Modification time (UTC).
        author_count (Optional[int]): Number of named persons among the authors.
        is_agency_copy (Optional[bool]): Whether an agency is credited.
        word_count (Optional[int]): Word count from the JSON-LD.
        is_paywalled (Optional[bool]): True if the JSON-LD marks the article as not free.
        has_video (bool): Whether the page embeds a video.
        lead_text (str): First paragraphs of the article, up to LEAD_TEXT_CHARS characters.
        tdm_reserved (bool): Whether a tdm-reservation meta tag declares a reservation.
        has_article_data (bool): Whether an article object was found in the JSON-LD.
    """
    canonical_url: Optional[str] = None
    jsonld_type: Optional[str] = None
    section: Optional[str] = None
    keywords: list[str] = field(default_factory=list)
    description: Optional[str] = None
    published_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    author_count: Optional[int] = None
    is_agency_copy: Optional[bool] = None
    word_count: Optional[int] = None
    is_paywalled: Optional[bool] = None
    has_video: bool = False
    lead_text: str = ""
    tdm_reserved: bool = False
    has_article_data: bool = False


def clean_text(value: Any, limit: Optional[int] = None) -> str:
    """Removes HTML tags, collapses whitespace and shortens a text.

    Args:
        value: Text or HTML; non-strings give an empty string.
        limit: Maximum number of characters; None keeps the full text.

    Returns:
        str: The cleaned text.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ") if "<" in value else value
    collapsed = " ".join(text.split())
    if limit is not None and len(collapsed) > limit:
        return collapsed[: limit - 3].rstrip() + "..."
    return collapsed


def parse_datetime(value: Any) -> Optional[datetime]:
    """Parses an ISO 8601 or RFC 2822 time and converts it to UTC.

    Args:
        value: The time as text.

    Returns:
        Optional[datetime]: The time in UTC without microseconds; times without
        a zone are taken as UTC. None if the value cannot be parsed.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _types(node: dict[str, Any]) -> set[str]:
    """Returns the schema.org types of a JSON-LD node."""
    value = node.get("@type")
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    """Yields all JSON-LD objects in a parsed document, including those in @graph and lists."""
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "hasPart"):
            if key in node:
                yield from _walk(node[key])


def _jsonld_nodes(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Parses all JSON-LD scripts of a page; invalid scripts are skipped."""
    nodes: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            parsed = json.loads(raw.strip())
        except ValueError:
            continue
        nodes.extend(_walk(parsed))
    return nodes


def _as_list(value: Any) -> list[Any]:
    """Wraps a single value in a list; None gives an empty list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _keywords(value: Any) -> list[str]:
    """Normalizes JSON-LD keywords (comma-separated text or list) to a list without duplicates."""
    items: list[str] = []
    for entry in _as_list(value):
        if isinstance(entry, str):
            items.extend(part.strip() for part in entry.split(","))
    return list(dict.fromkeys(item for item in items if item))[:30]


def _authors(article: dict[str, Any]) -> tuple[int, bool]:
    """Counts the named persons among the authors and tells whether an agency is credited.

    Args:
        article: The JSON-LD article object.

    Returns:
        tuple[int, bool]: Number of persons and whether an agency appears among authors or creators.
    """
    persons = 0
    agency = False
    for key in ("author", "creator", "sourceOrganization"):
        for author in _as_list(article.get(key)):
            name = author.get("name") if isinstance(author, dict) else author
            if not isinstance(name, str) or not name.strip():
                continue
            if AGENCY_PATTERN.search(name):
                agency = True
            elif key == "author" and (not isinstance(author, dict) or "Organization" not in _types(author)):
                persons += 1
    return persons, agency


def _is_free(value: Any) -> Optional[bool]:
    """Interprets the JSON-LD isAccessibleForFree value."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _lead_from_html(soup: BeautifulSoup) -> str:
    """Joins the first substantial paragraphs of the article element of a page."""
    container = soup.find("article") or soup.find("main") or soup.body
    if container is None:
        return ""
    parts: list[str] = []
    length = 0
    for paragraph in container.find_all("p"):
        text = " ".join(paragraph.get_text(" ").split())
        if len(text) < MIN_PARAGRAPH_CHARS:
            continue
        parts.append(text)
        length += len(text)
        if length >= LEAD_TEXT_CHARS:
            break
    return clean_text(" ".join(parts), LEAD_TEXT_CHARS)


def parse_page(html: str) -> PageInfo:
    """Extracts metadata and lead text from an article page.

    Args:
        html: The HTML of the page.

    Returns:
        PageInfo: The extracted values; missing values stay None or empty.
    """
    soup = BeautifulSoup(html, "html.parser")
    info = PageInfo()
    canonical = soup.find("link", rel="canonical")
    if canonical is not None and canonical.get("href"):
        info.canonical_url = str(canonical["href"]).strip()
    tdm_meta = soup.find("meta", attrs={"name": "tdm-reservation"})
    info.tdm_reserved = tdm_meta is not None and str(tdm_meta.get("content", "")).strip() == "1"
    nodes = _jsonld_nodes(soup)
    article = next((node for node in nodes if _types(node) & ARTICLE_TYPES), None)
    info.has_video = (soup.find("video") is not None
                      or soup.find("meta", attrs={"property": "og:video"}) is not None
                      or any("VideoObject" in _types(node) for node in nodes))
    if article is not None:
        info.has_article_data = True
        known = sorted(_types(article) & ARTICLE_TYPES)
        info.jsonld_type = known[0] if known else None
        sections = [value for value in _as_list(article.get("articleSection")) if isinstance(value, str)]
        info.section = (clean_text(sections[0], 80) or None) if sections else None
        info.keywords = _keywords(article.get("keywords"))
        info.description = clean_text(article.get("description"), MAX_TEASER_CHARS) or None
        info.published_at = parse_datetime(article.get("datePublished"))
        info.updated_at = parse_datetime(article.get("dateModified"))
        info.author_count, info.is_agency_copy = _authors(article)
        word_count = article.get("wordCount")
        info.word_count = int(word_count) if isinstance(word_count, (int, str)) and str(word_count).isdigit() else None
        free = _is_free(article.get("isAccessibleForFree"))
        info.is_paywalled = None if free is None else not free
        info.has_video = info.has_video or bool(article.get("video"))
        info.lead_text = clean_text(article.get("articleBody"), LEAD_TEXT_CHARS)
    if not info.keywords:
        meta_keywords = soup.find("meta", attrs={"name": "keywords"})
        if meta_keywords is not None:
            info.keywords = _keywords(meta_keywords.get("content"))
    if info.description is None:
        meta_description = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", attrs={"property": "og:description"})
        if meta_description is not None:
            info.description = clean_text(meta_description.get("content"), MAX_TEASER_CHARS) or None
    if not info.lead_text:
        info.lead_text = _lead_from_html(soup)
    if info.lead_text and DATELINE_PATTERN.search(info.lead_text[:120]):
        info.is_agency_copy = True
    return info
