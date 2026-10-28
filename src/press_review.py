"""
Review of the news outlets and press sources in ``config/sources.toml``, part of the monthly source review.

The review
1. checks the active sources: their health in ``state/sources.json``, their
   yield of sociodemographic items in the last REVIEW_WINDOW_DAYS days, every
   feed and news sitemap (errors, permanent redirects, age of the newest item)
   and the robots.txt of their homepage (crawl delay, a text-and-data-mining
   reservation stated in natural language);
2. finds gaps: states without a press source of their statistical office,
   states without a regional daily, and topics without a specialist outlet;
3. asks Gemini with Google Search for the official feeds of the federal
   institutions for population, migration, family, education, social affairs,
   health, housing and justice, of research institutes, welfare associations,
   religious communities and other organizations, and of the statistical
   offices, regional dailies and specialist outlets of step 2, and checks every
   candidate with the fetch rules of the pipeline;
4. writes the proposed changes into the configuration (removed dates, feed
   changes, page fetching, new sources, exclusions of rejected candidates) and
   returns a section of the Markdown report.

``source_review.py`` runs this review after the review of the statistical
sources; the workflow ``source_review.yml`` opens a pull request with the
changed configuration and the report, and the changes take effect when it is
merged. Every request goes through ``WebClient`` and its fetch rules. Hosts
that blocked automated requests or reserved text and data mining are not
requested again.
"""
import json
import logging
import re
import tempfile
import time
import warnings
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional, Union, get_args
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from google.genai import errors, types
from pydantic import BaseModel, ConfigDict, ValidationError

from annotation import create_client
from collectors.feeds import (
    MAX_CHILD_SITEMAPS,
    SOCIAL_PATH_TOKENS,
    Candidate,
    looks_sociodemographic,
    parse_feed,
    parse_news_sitemap,
)
from content import parse_page
from schema import PaywallModel, StateCode
from settings import Settings
from sources import (
    ExcludedSource,
    Outlet,
    PressSource,
    Source,
    SourceConfig,
    SourceHealth,
    SourceHealthStore,
    load_config,
)
from urls import domain_id, normalize_url, path_tokens
from web import (
    MAX_CRAWL_DELAY_S,
    BlockedError,
    FetchError,
    NotFoundError,
    RobotsDisallowedError,
    RobotsRules,
    WebClient,
    host_of,
)

logger = logging.getLogger(__name__)

# Sources without any sociodemographic item in this window are proposed for removal (unless keep = true)
REVIEW_WINDOW_DAYS = 90
# A feed whose newest item is older than this is stale
STALE_FEED_DAYS = 180
# Article pages are fetched only from hosts with at most this crawl delay, so that a run stays short
MAX_PAGE_CRAWL_DELAY_S = 10.0
MAX_FEEDS_PER_SOURCE = 3
MAX_FEEDS_CHECKED_PER_CANDIDATE = 12
# Feed links read from one page (head links and links whose address looks like a feed)
MAX_FEED_LINKS_PER_PAGE = 20
MAX_ADDITIONS_PER_REVIEW = 5
MAX_SEARCH_CALLS = 5
MAX_CANDIDATES_PER_SEARCH = 12
# Topics without a specialist outlet that are searched in one review
MAX_TOPICS_PER_SEARCH = 6
MIN_SEARCH_INTERVAL_S = 2.0
SEARCH_TEMPERATURE = 0.2
SEARCH_TIMEOUT_MS = 120_000
# A new press source needs at least one item in PRESS_RECENT_DAYS days; a new outlet at least
# MIN_OUTLET_ITEMS sociodemographic items in OUTLET_RECENT_DAYS days
PRESS_RECENT_DAYS = 60
OUTLET_RECENT_DAYS = 7
MIN_OUTLET_ITEMS = 5
# Requests stop after this time, so that the review of the statistical sources and of the press ends within the
# timeout of the workflow
REVIEW_TIME_BUDGET_S = 20 * 60
# Notes beyond this number are counted, not listed, so that the pull request description stays short
MAX_REPORT_NOTES = 40
ITEM_DATASETS = ("news_items", "press_releases")
# Topics for which a specialist outlet is searched when the list has none
SPECIALIST_TOPICS: tuple[str, ...] = (
    "Migration and asylum", "Education and schools", "Religion and churches", "Health and care",
    "Families and households", "Income, poverty and social benefits", "Housing and rents", "Older people and ageing",
)
# States whose statistics are published by the office of another state: Statistikamt Nord (Hamburg and
# Schleswig-Holstein) and the Amt für Statistik Berlin-Brandenburg
SHARED_STATISTICAL_OFFICES: dict[str, str] = {"DE-SH": "DE-HH", "DE-BB": "DE-BE"}
FEED_TYPES = ("application/rss+xml", "application/atom+xml")
PRESS_URL_TOKENS = ("presse", "press", "mitteilung", "meldung", "news", "aktuell")
# Link addresses that look like a feed or an overview page of feeds, e.g. /rss.xml, /service/rss-feeds/, /feed/
FEED_LINK_PATTERN = re.compile(r"(?<![a-z])(rss|atom)(?![a-z])|feed(?!back)|\.xml(?:\?|$)", re.IGNORECASE)

SEARCH_RULES = """\
Rules:
- List only feeds on the official website of the institution, organization or publication, no aggregators, no press release distribution services and no social media.
- Give the feed URLs as found in the search results or on the website; do not construct them.
- Do not suggest these domains, which are already known: {known}."""

FEDERAL_PROMPT = """\
Use Google Search to find the official RSS or Atom feeds with press releases of the German federal institutions for population, migration, integration, family, education, social affairs, health, housing and justice and their statistics, in their current composition: the federal ministries responsible for the interior, family and education, labour and social affairs, health, housing and justice, the Statistisches Bundesamt, the Bundesamt für Migration und Flüchtlinge, the Bundesinstitut für Bevölkerungsforschung, the Bundesagentur für Arbeit, the Bundeskriminalamt, the Robert Koch-Institut and the commissioners of the federal government for migration, refugees and integration. Ministries are renamed and restructured after elections; use their current names and websites.

These institutions are already in the list; do not suggest them again, also not under an earlier name or domain: {listed}.

{rules}

Return only a JSON array without other text, with one object per institution:
[{{"name": "current official German name", "homepage": "https://...", "feeds": ["https://..."], "actor_type": "federal_ministry, public_agency or statistical_office"}}]"""

ASSOCIATIONS_PROMPT = """\
Use Google Search to find the official RSS or Atom feeds with press releases of German nationwide research institutes on demography, migration, families, education, social policy and inequality, of the welfare associations, of the churches and other religious communities, of civil society organizations on migration, children, older people, housing and poverty, of trade unions and of the associations of the municipalities.

These are already in the list; do not suggest them again: {listed}.

{rules}

Return only a JSON array without other text, with up to {count} objects:
[{{"name": "official German name", "homepage": "https://...", "feeds": ["https://..."], "actor_type": "research_institute, welfare_association, religious_community, civil_society_organization, trade_union or municipal_association"}}]"""

STATISTICAL_OFFICES_PROMPT = """\
Use Google Search to find the official RSS or Atom feeds with press releases of the statistical offices (Statistische Landesämter) of these German states:
{states}

{rules}

Return only a JSON array without other text, with one object per state:
[{{"name": "official German name of the office", "homepage": "https://...", "feeds": ["https://..."], "jurisdiction": "the state code given in the list"}}]"""

REGIONAL_PRESS_PROMPT = """\
Use Google Search to find regional daily newspapers of these German states that publish an RSS or Atom feed of their news or a Google News sitemap:
{states}

{rules}

Return only a JSON array without other text, with up to {count} objects:
[{{"name": "name of the newspaper", "homepage": "https://...", "feeds": ["https://..."], "news_sitemap": "https://... or null", "home_state": "the state code given in the list", "owner_group": "publisher", "paywall_model": "free, mixed or hard"}}]"""

SPECIALIST_PRESS_PROMPT = """\
Use Google Search to find German-language specialist publications (Fachpresse) with news on these topics that publish an RSS or Atom feed of their news or a Google News sitemap:
{topics}

{rules}

Return only a JSON array without other text, with up to {count} objects:
[{{"name": "name of the publication", "homepage": "https://...", "feeds": ["https://..."], "news_sitemap": "https://... or null", "topic": "the topic given in the list", "owner_group": "publisher", "paywall_model": "free, mixed or hard"}}]"""

ChangeAction = Literal["remove_source", "remove_feed", "replace_feed", "remove_sitemap", "set_fetch_pages",
                       "add_source", "exclude", "drop_exclusion"]
SearchCategory = Literal["federal", "associations", "statistical_offices", "regional_press", "specialist_press"]
FEDERAL_ACTOR_TYPES = ("federal_government", "federal_ministry", "parliament", "public_agency", "statistical_office")
ASSOCIATION_ACTOR_TYPES = ("research_institute", "welfare_association", "religious_community",
                           "civil_society_organization", "trade_union", "municipal_association")
# Issuers whose releases cover many other topics; their items need a sociodemographic section or term
BROAD_ACTOR_TYPES = frozenset({"federal_government", "parliament", "state_government", "trade_union",
                               "municipal_association"})
STATES: tuple[str, ...] = get_args(StateCode)
STATE_NAMES: dict[str, str] = {
    "DE-BW": "Baden-Württemberg", "DE-BY": "Bayern", "DE-BE": "Berlin", "DE-BB": "Brandenburg", "DE-HB": "Bremen",
    "DE-HH": "Hamburg", "DE-HE": "Hessen", "DE-MV": "Mecklenburg-Vorpommern", "DE-NI": "Niedersachsen",
    "DE-NW": "Nordrhein-Westfalen", "DE-RP": "Rheinland-Pfalz", "DE-SL": "Saarland", "DE-SN": "Sachsen",
    "DE-ST": "Sachsen-Anhalt", "DE-SH": "Schleswig-Holstein", "DE-TH": "Thüringen",
}


class SearchError(Exception):
    """A search request failed or the search budget is used up."""


@dataclass(frozen=True)
class NewEntry:
    """A new entry of the configuration.

    Attributes:
        table (str): outlet, press_source or excluded.
        fields (dict[str, Any]): Keys and values in the order of the entry; None values are left out.
        comment (Optional[str]): Comment line above the entry.
    """
    table: str
    fields: dict[str, Any]
    comment: Optional[str] = None


@dataclass(frozen=True)
class Change:
    """A proposed change of the configuration.

    Attributes:
        action (str): What to change (see ``ChangeAction``).
        source_id (str): Id of the source or exclusion.
        reason (str): Why; shown in the pull request and in the configuration.
        old (Optional[str]): Feed or sitemap URL that is removed or replaced.
        new (Optional[str]): Feed URL that replaces ``old``.
        entry (Optional[NewEntry]): The new entry of ``add_source`` and ``exclude``.
    """
    action: ChangeAction
    source_id: str
    reason: str
    old: Optional[str] = None
    new: Optional[str] = None
    entry: Optional[NewEntry] = None


@dataclass(frozen=True)
class FeedCheck:
    """Result of fetching a feed or news sitemap.

    Attributes:
        url (str): The URL.
        outcome (str): ok, not_found, disallowed, blocked, error or invalid.
        detail (str): Description of a failure.
        entries (tuple[Candidate, ...]): The items of the feed.
        newest (Optional[datetime]): Publication time of the newest item.
        moved_to (Optional[str]): New URL after permanent redirects.
        links (tuple[str, ...]): Feed links of an HTML page that is not a feed, e.g. an
            overview page of the feeds of a ministry.
    """
    url: str
    outcome: Literal["ok", "not_found", "disallowed", "blocked", "error", "invalid"]
    detail: str = ""
    entries: tuple[Candidate, ...] = ()
    newest: Optional[datetime] = None
    moved_to: Optional[str] = None
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class Gaps:
    """Parts of the coverage without a source.

    Attributes:
        statistical_states (tuple[str, ...]): States without a press source of their statistical office.
        regional_states (tuple[str, ...]): States without a regional daily.
        topics (tuple[str, ...]): Topics of SPECIALIST_TOPICS without a specialist outlet.
    """
    statistical_states: tuple[str, ...] = ()
    regional_states: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rejection:
    """A candidate that was not admitted.

    Attributes:
        name (str): Name of the candidate.
        homepage (str): Homepage URL.
        reason (str): Why it was not admitted.
        status (Optional[str]): Exclusion status (see ``ExclusionStatus``); None if the
            reason may be temporary, so that the candidate is not excluded.
        kind (Optional[str]): outlet or press_source, if the attributes were valid.
        attributes (dict[str, Any]): The validated attributes, kept with an exclusion for its recheck.
        feeds (tuple[str, ...]): The proposed feed URLs.
    """
    name: str
    homepage: str
    reason: str
    status: Optional[str] = None
    kind: Optional[str] = None
    attributes: dict[str, Any] = field(default_factory=dict)
    feeds: tuple[str, ...] = ()


@dataclass
class ReviewResult:
    """Outcome of a review.

    Attributes:
        changes (list[Change]): Proposed changes of the configuration.
        notes (list[str]): Observations without a change, e.g. temporary errors.
        rejections (list[Rejection]): Candidates that were not admitted.
        gaps (Gaps): Gaps that were searched for.
        searches (list[str]): Search categories that were sent to Gemini.
        checked_sources (int): Active sources that were checked.
        candidates (int): Candidates from the search that were evaluated.
        rechecked (int): Excluded candidates that were checked again.
        selected (bool): True if only selected sources were checked, without gaps, rechecks and search.
        checks (list[tuple[str, str]]): Source id and a summary of its feeds (items, newest item), so that
            a new source can be verified from the report.
    """
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    gaps: Gaps = field(default_factory=Gaps)
    searches: list[str] = field(default_factory=list)
    checked_sources: int = 0
    candidates: int = 0
    rechecked: int = 0
    selected: bool = False
    checks: list[tuple[str, str]] = field(default_factory=list)


class PressCandidate(BaseModel):
    """A press-release source proposed by the search."""
    model_config = ConfigDict(extra="ignore")

    name: str
    homepage: str
    feeds: list[str] = []
    actor_type: Optional[str] = None
    jurisdiction: Optional[str] = None


class OutletCandidate(BaseModel):
    """A news outlet proposed by the search."""
    model_config = ConfigDict(extra="ignore")

    name: str
    homepage: str
    feeds: list[str] = []
    news_sitemap: Optional[str] = None
    home_state: Optional[str] = None
    topic: Optional[str] = None
    owner_group: Optional[str] = None
    paywall_model: Optional[str] = None


SearchCandidate = Union[PressCandidate, OutletCandidate]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class SearchModel:
    """Gemini with Google Search grounding, for short JSON answers.

    Attributes:
        calls (int): Requests sent in this review.
    """

    def __init__(self, settings: Settings, client: Optional[Any] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        """Initializes the model.

        Args:
            settings: The run settings.
            client: A ``genai.Client`` or a compatible test double; created from the settings' API key if omitted.
            sleep: Function used to wait between requests; replaceable in tests.
        """
        self._client = client if client is not None else create_client(settings.gemini_api_key,
                                                                         timeout_ms=SEARCH_TIMEOUT_MS)
        self._model = settings.gemini_model
        self._sleep = sleep
        self.calls = 0

    def ask(self, prompt: str) -> str:
        """Sends a prompt with Google Search enabled.

        Args:
            prompt: The prompt.

        Returns:
            str: The answer text; empty if the model returned none.

        Raises:
            SearchError: If MAX_SEARCH_CALLS requests were sent or the request failed.
        """
        if self.calls >= MAX_SEARCH_CALLS:
            raise SearchError(f"MAX_SEARCH_CALLS ({MAX_SEARCH_CALLS}) reached")
        if self.calls:
            self._sleep(MIN_SEARCH_INTERVAL_S)
        self.calls += 1
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=SEARCH_TEMPERATURE,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            response = self._client.models.generate_content(model=self._model, contents=prompt, config=config)
        except errors.APIError as e:
            raise SearchError(f"Gemini request failed: {e}") from e
        except httpx.TransportError as e:
            raise SearchError(f"Network error after retries: {e!r}") from e
        usage = getattr(response, "usage_metadata", None)
        logger.info("Search %d: prompt %s tokens, output %s, thinking %s.", self.calls,
                    getattr(usage, "prompt_token_count", None), getattr(usage, "candidates_token_count", None),
                    getattr(usage, "thoughts_token_count", None))
        return getattr(response, "text", None) or ""


def parse_json_array(text: str) -> list[Any]:
    """Extracts the JSON array from an answer that may contain code fences or other text.

    Args:
        text: The answer.

    Returns:
        list: The parsed array.

    Raises:
        ValueError: If the answer contains no valid JSON array.
    """
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end < start:
        raise ValueError("the answer contains no JSON array")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("the answer is not a JSON array")
    return data


def parse_candidates(text: str, model: type[BaseModel]) -> list[Any]:
    """Parses a JSON array answer into candidates; invalid items are skipped.

    Args:
        text: The answer.
        model: ``PressCandidate`` or ``OutletCandidate``.

    Returns:
        list: The valid candidates, at most MAX_CANDIDATES_PER_SEARCH.

    Raises:
        ValueError: If the answer contains no valid JSON array.
    """
    candidates = []
    for item in parse_json_array(text):
        try:
            candidates.append(model.model_validate(item))
        except ValidationError:
            logger.info("Skipped an invalid candidate: %r", item)
    return candidates[:MAX_CANDIDATES_PER_SEARCH]


# ---------------------------------------------------------------------------
# Checks of the active sources
# ---------------------------------------------------------------------------

def count_items(data_dir: Path, since: date) -> Counter[str]:
    """Counts the stored news items and press releases per source, observed on or after a day.

    Args:
        data_dir: Root folder of the datasets.
        since: First day to count.

    Returns:
        Counter[str]: Records per source id.
    """
    counts: Counter[str] = Counter()
    cutoff = since.isoformat()
    for dataset in ITEM_DATASETS:
        for path in sorted((data_dir / dataset).glob("year=*/month=*/*.json")):
            # Files are partitioned by the day of the observing run: <dataset>_YYYY-MM-DD.json
            if path.stem.rsplit("_", 1)[-1] < cutoff:
                continue
            for record in json.loads(path.read_text(encoding="utf-8")):
                counts[str(record.get("source_id"))] += 1
    return counts


def check_feed(web: WebClient, url: str, source: Source, sitemap: bool = False) -> FeedCheck:
    """Fetches and parses a feed or news sitemap.

    Args:
        web: The HTTP client.
        url: URL of the feed or sitemap.
        source: The source the items belong to.
        sitemap: True for a news sitemap; the first child sitemaps of an index are read as well.

    Returns:
        FeedCheck: The outcome and the items.
    """
    try:
        response = web.get(url)
    except RobotsDisallowedError:
        return FeedCheck(url, "disallowed", "disallowed by robots.txt")
    except NotFoundError as e:
        return FeedCheck(url, "not_found", e.reason)
    except BlockedError as e:
        return FeedCheck(url, "blocked", e.reason)
    except FetchError as e:
        return FeedCheck(url, "error", e.reason)
    try:
        if sitemap and isinstance(source, Outlet):
            entries, children = parse_news_sitemap(response.content, source, url)
            for child in children[:MAX_CHILD_SITEMAPS]:
                try:
                    entries.extend(parse_news_sitemap(web.get(child).content, source, child)[0])
                except (FetchError, ValueError) as e:
                    logger.info("Child sitemap %s failed: %s", child, e)
        else:
            entries = parse_feed(response.content, source, url)
            if not entries and "html" in response.content_type:
                raise ValueError("not a feed (HTML page)")
    except ValueError as e:
        links = tuple(feed_links(response.text, response.url)) if "html" in response.content_type else ()
        return FeedCheck(url, "invalid", str(e)[:160], links=links)
    dates = [entry.published_at for entry in entries if entry.published_at is not None]
    moved_to = response.url if response.moved_permanently and response.url != url else None
    return FeedCheck(url, "ok", entries=tuple(entries), newest=max(dates, default=None), moved_to=moved_to)


def prefetch_robots(web: WebClient, urls: Iterable[str]) -> None:
    """Loads the robots.txt of every host first, so that crawl delays pass while the other hosts are read.

    Args:
        web: The HTTP client.
        urls: URLs of the hosts.
    """
    by_host: dict[str, str] = {}
    for url in urls:
        by_host.setdefault(host_of(url), url)
    for url in by_host.values():
        try:
            web.allowed(url)
        except FetchError:
            continue


def _source_urls(source: Source) -> list[str]:
    """Homepage, feeds and news sitemap of a source."""
    sitemap = source.news_sitemap if isinstance(source, Outlet) else None
    return [source.homepage, *source.feeds, *([sitemap] if sitemap else [])]


def lacks_yield(source: Source, items: int, today: date) -> bool:
    """Tells whether a source is proposed for removal because it yielded no sociodemographic item.

    Args:
        source: The source.
        items: Its records in the last REVIEW_WINDOW_DAYS days.
        today: Day of the review.

    Returns:
        bool: True if the source was added at least REVIEW_WINDOW_DAYS days ago, has no
        records in that window and is not marked with ``keep = true``.
    """
    if source.keep or source.added is None or items:
        return False
    return (today - source.added).days >= REVIEW_WINDOW_DAYS


def summarize_check(check: FeedCheck, label: str) -> str:
    """Describes the result of a feed check in one line of the report.

    Args:
        check: The result.
        label: "feed" or "news sitemap".

    Returns:
        str: For example ``feed https://...: 40 items, 12 pass the topic filter, newest 2026-10-27``.
    """
    if check.outcome != "ok":
        return f"{label} {check.url}: {check.outcome} ({check.detail})"
    passing = sum(1 for entry in check.entries if looks_sociodemographic(entry))
    newest = check.newest.date().isoformat() if check.newest else "no dates"
    return f"{label} {check.url}: {len(check.entries)} items, {passing} pass the topic filter, newest {newest}"


def review_source(web: WebClient, source: Source, health: SourceHealth, items: int,
                  now: datetime) -> tuple[list[Change], list[str], str]:
    """Checks one active source.

    A source is proposed for removal if it is blocked or TDM reserved, has
    no working feed, is broken and still fails, requires a crawl delay above
    MAX_CRAWL_DELAY_S or yielded no sociodemographic item in REVIEW_WINDOW_DAYS
    days. Otherwise single feeds are removed or replaced, and page fetching is
    turned off where robots.txt states a TDM reservation or a long crawl delay.

    Args:
        web: The HTTP client.
        source: The source.
        health: Its health from ``state/sources.json``.
        items: Its records in the last REVIEW_WINDOW_DAYS days.
        now: Time of the review (UTC).

    Returns:
        tuple[list[Change], list[str], str]: The proposed changes, notes and a summary of the feeds.
    """
    if health.status in ("blocked", "tdm_reserved"):
        # These hosts are not requested again
        reason = f"{health.status.replace('_', ' ')} since {health.last_attempt or 'unknown'}"
        if health.last_error:
            reason += f": {health.last_error}"
        return [Change("remove_source", source.id, reason)], [], "not requested: " + reason
    if lacks_yield(source, items, now.date()):
        reason = f"no sociodemographic items in the last {REVIEW_WINDOW_DAYS} days"
        return [Change("remove_source", source.id, reason)], [], "not requested: " + reason

    changes: list[Change] = []
    notes: list[str] = []
    checks = [check_feed(web, url, source) for url in source.feeds]
    sitemap_check = None
    if isinstance(source, Outlet) and source.news_sitemap:
        sitemap_check = check_feed(web, source.news_sitemap, source, sitemap=True)
    all_checks = [*checks, *([sitemap_check] if sitemap_check else [])]
    summary = "; ".join(summarize_check(check, "news sitemap" if check is sitemap_check else "feed")
                        for check in all_checks) or "no feeds"
    blocked = next((check for check in all_checks if check.outcome == "blocked"), None)
    if blocked is not None:
        return [Change("remove_source", source.id, f"blocked: {blocked.detail} ({blocked.url})")], [], summary

    stale_before = now - timedelta(days=STALE_FEED_DAYS)
    dropped: list[str] = []
    for check in all_checks:
        is_sitemap = check is sitemap_check
        label = "news sitemap" if is_sitemap else "feed"
        reason: Optional[str] = None
        if check.outcome in ("not_found", "disallowed"):
            reason = check.detail
        elif check.outcome == "ok" and check.newest is not None and check.newest < stale_before:
            reason = f"newest item from {check.newest.date().isoformat()}"
        if reason is not None:
            changes.append(Change("remove_sitemap" if is_sitemap else "remove_feed", source.id, reason, old=check.url))
            dropped.append(f"{label} {check.url}: {reason}")
        elif check.outcome == "ok" and check.moved_to and not is_sitemap:
            changes.append(Change("replace_feed", source.id, "moved permanently", old=check.url, new=check.moved_to))
        elif check.outcome in ("error", "invalid"):
            notes.append(f"`{source.id}`: {label} {check.url}: {check.detail}")
        elif check.outcome == "ok" and not check.entries:
            notes.append(f"`{source.id}`: {label} {check.url} has no items")
    if len(dropped) == len(all_checks):
        return [Change("remove_source", source.id, "no working feed: " + "; ".join(dropped))], notes, summary
    working = any(check.outcome == "ok" for check in all_checks)
    if health.status == "broken":
        if not working:
            reason = f"broken since {health.last_success or 'the first run'}: {health.last_error or 'feeds fail'}"
            return [Change("remove_source", source.id, reason)], notes, summary
        notes.append(f"`{source.id}`: broken in sources.json, but a feed works again")

    try:
        rules: Optional[RobotsRules] = web.robots_rules(source.homepage)
    except FetchError as e:
        notes.append(f"`{source.id}`: robots.txt of the homepage not checked ({e.reason})")
        rules = None
    delay = rules.crawl_delay if rules else None
    if delay is not None and delay > MAX_CRAWL_DELAY_S:
        return [Change("remove_source", source.id, f"robots.txt crawl delay {delay:g} s, more than "
                                                    f"{MAX_CRAWL_DELAY_S:g} s")], notes, summary
    if source.fetch_pages and rules is not None and rules.tdm_statement:
        changes.append(Change("set_fetch_pages", source.id,
                              "robots.txt states a text-and-data-mining reservation in natural language"))
    elif source.fetch_pages and delay is not None and delay > MAX_PAGE_CRAWL_DELAY_S:
        changes.append(Change("set_fetch_pages", source.id, f"robots.txt crawl delay {delay:g} s"))
    return changes, notes, summary


# ---------------------------------------------------------------------------
# Gaps and candidates
# ---------------------------------------------------------------------------

def find_gaps(config: SourceConfig, leaving: frozenset[str] = frozenset()) -> Gaps:
    """Finds the states and topics without a source.

    Args:
        config: The configuration.
        leaving: Ids of sources that the review proposes to remove; they do not count.

    Returns:
        Gaps: States without a press source of their statistical office (a state whose statistics are
        published by the office of another state counts as covered by that office), states without a
        regional daily, and topics of SPECIALIST_TOPICS without a specialist outlet.
    """
    active = [source for source in config.active_sources if source.id not in leaving]
    offices = {source.jurisdiction for source in active
               if isinstance(source, PressSource) and source.actor_type == "statistical_office"}
    dailies = {source.home_state for source in active
               if isinstance(source, Outlet) and source.outlet_type == "regional_daily"}
    topics = {source.topic for source in active if isinstance(source, Outlet) and source.topic}
    return Gaps(
        statistical_states=tuple(state for state in STATES
                                 if state not in offices and SHARED_STATISTICAL_OFFICES.get(state) not in offices),
        regional_states=tuple(state for state in STATES if state not in dailies),
        topics=tuple(topic for topic in SPECIALIST_TOPICS if topic not in topics)[:MAX_TOPICS_PER_SEARCH],
    )


def known_hosts(config: SourceConfig) -> set[str]:
    """Returns the hosts that the search must not propose.

    Args:
        config: The configuration.

    Returns:
        set[str]: Hosts (without ``www.``) of all sources, including removed ones, and of all
        exclusions; the review checks the exclusions with a temporary reason itself.
    """
    hosts: set[str] = set()
    for source in config.sources:
        for url in _source_urls(source):
            try:
                hosts.add(domain_id(url))
            except ValueError:
                continue
    hosts.update(domain_id(entry.homepage) for entry in config.excluded)
    return hosts


def known_feeds(config: SourceConfig) -> dict[str, str]:
    """Returns the listed feeds and news sitemaps, so that a candidate with the same feed is recognized.

    Args:
        config: The configuration.

    Returns:
        dict[str, str]: Source id by normalized URL, for all sources including removed ones.
    """
    feeds: dict[str, str] = {}
    for source in config.sources:
        for url in _source_urls(source)[1:]:
            try:
                feeds.setdefault(normalize_url(url), source.id)
            except ValueError:
                continue
    return feeds


def _state_lines(states: Sequence[str]) -> str:
    """Lists states with their names and codes for a prompt."""
    return "\n".join(f"- {STATE_NAMES.get(state, state)} ({state})" for state in states)


def search_candidates(model: Any, gaps: Gaps, known: set[str], result: ReviewResult, listed_federal: Sequence[str] = (),
                      listed_associations: Sequence[str] = ()) -> list[tuple[SearchCategory, SearchCandidate]]:
    """Asks the search for candidates: federal institutions and associations always, the gaps where there are any.

    Args:
        model: A ``SearchModel`` or a compatible test double.
        gaps: The gaps.
        known: Hosts that must not be proposed.
        result: The review result; searches and notes are added.
        listed_federal: Names of the federal institutions in the list.
        listed_associations: Names of the institutes, associations and organizations in the list.

    Returns:
        list: Category and candidate, in the order of the answers.
    """
    rules = SEARCH_RULES.format(known=", ".join(sorted(known)) or "(none)")
    requests: list[tuple[SearchCategory, str, type[BaseModel]]] = [
        ("federal", FEDERAL_PROMPT.format(rules=rules, listed="; ".join(listed_federal) or "(none)"), PressCandidate),
        ("associations", ASSOCIATIONS_PROMPT.format(rules=rules, listed="; ".join(listed_associations) or "(none)",
                                                    count=MAX_CANDIDATES_PER_SEARCH), PressCandidate),
    ]
    if gaps.statistical_states:
        requests.append(("statistical_offices", STATISTICAL_OFFICES_PROMPT.format(
            states=_state_lines(gaps.statistical_states), rules=rules), PressCandidate))
    if gaps.regional_states:
        requests.append(("regional_press", REGIONAL_PRESS_PROMPT.format(
            states=_state_lines(gaps.regional_states), rules=rules, count=MAX_CANDIDATES_PER_SEARCH), OutletCandidate))
    if gaps.topics:
        lines = "\n".join(f"- {topic}" for topic in gaps.topics)
        requests.append(("specialist_press", SPECIALIST_PRESS_PROMPT.format(
            topics=lines, rules=rules, count=MAX_CANDIDATES_PER_SEARCH), OutletCandidate))
    found: list[tuple[SearchCategory, SearchCandidate]] = []
    for category, prompt, candidate_model in requests:
        try:
            answer = model.ask(prompt)
        except SearchError as e:
            result.notes.append(f"Search `{category}` failed: {e}")
            break
        result.searches.append(category)
        try:
            found.extend((category, candidate) for candidate in parse_candidates(answer, candidate_model))
        except ValueError as e:
            result.notes.append(f"Search `{category}` returned no usable list ({e})")
    return found


def feed_links(html: str, base_url: str) -> list[str]:
    """Returns the feeds that a page links: the RSS and Atom links of its head, then the
    links whose address looks like a feed or an overview page of feeds (FEED_LINK_PATTERN).

    Government websites often list their feeds as normal links on an overview page.

    Args:
        html: The page.
        base_url: URL of the page, for relative links.

    Returns:
        list[str]: Absolute http(s) URLs without duplicates, at most MAX_FEED_LINKS_PER_PAGE.
    """
    # An XML document served as HTML is scanned for links as well; the warning of the HTML parser is not needed
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "html.parser")
    links = []
    for tag in soup.find_all("link", href=True):
        rel = tag.get("rel") or []
        rels = [value.lower() for value in (rel if isinstance(rel, list) else [rel])]
        if "alternate" in rels and str(tag.get("type") or "").lower() in FEED_TYPES:
            links.append(urljoin(base_url, str(tag["href"])))
    for tag in soup.find_all("a", href=True):
        url = urljoin(base_url, str(tag["href"]).strip())
        parts = urlsplit(url)
        target = f"{parts.path}?{parts.query}" if parts.query else parts.path
        if parts.scheme in ("http", "https") and FEED_LINK_PATTERN.search(target):
            links.append(url.split("#", 1)[0])
    return list(dict.fromkeys(links))[:MAX_FEED_LINKS_PER_PAGE]


def _new_id(base: str, taken: set[str]) -> str:
    """Returns a unique id from a base text and records it in ``taken``."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "source"
    candidate, number = cleaned, 2
    while candidate in taken:
        candidate, number = f"{cleaned}_{number}", number + 1
    taken.add(candidate)
    return candidate


def candidate_id(attributes: dict[str, Any], host: str, taken: set[str]) -> str:
    """Builds the id of a new source in the style of the configuration.

    Args:
        attributes: The validated attributes of the candidate.
        host: Host of its homepage.
        taken: Ids in use; the new id is added.

    Returns:
        str: For example ``statistik_nord`` or ``news4teachers``, from the host.
    """
    labels = host.split(".")
    return _new_id(labels[1] if labels[0] == "www" and len(labels) > 2 else labels[0], taken)


def _validated_attributes(category: SearchCategory, candidate: SearchCandidate, gaps: Gaps) -> dict[str, Any]:
    """Checks the attributes of a candidate against the vocabularies and the searched gap.

    Returns:
        dict[str, Any]: The attributes of the new entry.

    Raises:
        ValueError: If an attribute is missing, unknown or does not match the search.
    """
    if isinstance(candidate, OutletCandidate):
        paywall = candidate.paywall_model if candidate.paywall_model in get_args(PaywallModel) else "mixed"
        owner = " ".join((candidate.owner_group or "").split())[:80] or "unknown"
        if category == "regional_press":
            if candidate.home_state not in gaps.regional_states:
                raise ValueError(f"state {candidate.home_state!r} was not searched for")
            return {"outlet_type": "regional_daily", "owner_group": owner, "home_state": candidate.home_state,
                    "topic": None, "paywall_model": paywall}
        if category != "specialist_press" or candidate.topic not in gaps.topics:
            raise ValueError(f"topic {candidate.topic!r} was not searched for")
        return {"outlet_type": "specialist_press", "owner_group": owner, "home_state": None, "topic": candidate.topic,
                "paywall_model": paywall}
    if category == "statistical_offices":
        if candidate.jurisdiction not in gaps.statistical_states:
            raise ValueError(f"state {candidate.jurisdiction!r} was not searched for")
        return {"actor_type": "statistical_office", "jurisdiction": candidate.jurisdiction}
    if category == "federal":
        if candidate.actor_type not in FEDERAL_ACTOR_TYPES:
            raise ValueError(f"actor_type {candidate.actor_type!r} is not a federal institution")
        return {"actor_type": candidate.actor_type, "jurisdiction": "DE"}
    if category != "associations" or candidate.actor_type not in ASSOCIATION_ACTOR_TYPES:
        raise ValueError(f"actor_type {candidate.actor_type!r} is not an institute, association or organization")
    return {"actor_type": candidate.actor_type, "jurisdiction": "DE"}


def _feed_order(url: str, outlet: bool) -> tuple[int, str]:
    """Sort key that puts feeds of sociodemographic sections (outlets) or press feeds (press sources) first."""
    if outlet:
        preferred = bool(path_tokens(url) & SOCIAL_PATH_TOKENS)
    else:
        preferred = any(token in url.lower() for token in PRESS_URL_TOKENS)
    return (0 if preferred else 1, url)


def evaluate_candidate(web: WebClient, category: SearchCategory, candidate: SearchCandidate, gaps: Gaps,
                       now: datetime, taken: set[str], known: set[str], known_feeds: dict[str, str],
                       source_id: Optional[str] = None, out_of_time: Callable[[], bool] = lambda: False,
                       ) -> tuple[Optional[NewEntry], Optional[Rejection]]:
    """Checks a candidate with the fetch rules of the pipeline and builds its entry.

    The feeds are the proposed ones and those linked on the homepage; an HTML
    page among them, such as an overview page of the feeds of a ministry, is
    read for further feed links. A candidate is admitted if its robots.txt can
    be read, it declares no TDM reservation (TDMRep, header or meta tag), it
    does not block automated requests, it is not a source of the list under
    another domain (a homepage that redirects to a listed host, or only feeds
    that are listed), and its feeds are recent: a press source needs an item in the last
    PRESS_RECENT_DAYS days, an outlet MIN_OUTLET_ITEMS items that pass the
    topic filter in the last OUTLET_RECENT_DAYS days.

    Args:
        web: The HTTP client.
        category: Search category of the candidate.
        candidate: The candidate.
        gaps: The searched gaps.
        now: Time of the review (UTC).
        taken: Ids in use; the id of an admitted candidate is added.
        known: Hosts of the listed sources.
        known_feeds: Source id by normalized URL of every listed feed and news sitemap.
        source_id: Id of the new entry; built from the host if omitted.
        out_of_time: Tells whether the time budget of the review is used up; checked before every feed,
            so that a host with a long crawl delay cannot hold the review. A check stopped this way ends
            with a rejection without a status, which leaves the configuration unchanged.

    Returns:
        tuple: The new entry, or the rejection.
    """
    homepage = candidate.homepage.strip()
    proposed = tuple(dict.fromkeys(url.strip() for url in candidate.feeds if url.strip()))
    if urlsplit(homepage).scheme not in ("http", "https"):
        return None, Rejection(candidate.name, homepage, "homepage is not an http(s) URL")
    try:
        attributes = _validated_attributes(category, candidate, gaps)
    except ValueError as e:
        return None, Rejection(candidate.name, homepage, f"not used: {e}")
    outlet = isinstance(candidate, OutletCandidate)

    def reject(reason: str, status: Optional[str] = None) -> tuple[None, Rejection]:
        return None, Rejection(candidate.name, homepage, reason, status, "outlet" if outlet else "press_source",
                               attributes, proposed)

    provisional: Source
    if outlet:
        provisional = Outlet(id="candidate", name=candidate.name, homepage=homepage,
                             outlet_type=attributes["outlet_type"], owner_group=attributes["owner_group"],
                             paywall_model=attributes["paywall_model"], feeds=(), home_state=attributes["home_state"],
                             topic=attributes["topic"], topic_filter=attributes["outlet_type"] == "regional_daily")
    else:
        provisional = PressSource(id="candidate", name=candidate.name, homepage=homepage,
                                  actor_type=attributes["actor_type"], jurisdiction=attributes["jurisdiction"],
                                  feeds=(), topic_filter=attributes["actor_type"] in BROAD_ACTOR_TYPES)
    try:
        rules = web.robots_rules(homepage)
    except FetchError as e:
        return reject(f"robots.txt not readable ({e.reason})")
    delay = rules.crawl_delay if rules else None
    if delay is not None and delay > MAX_CRAWL_DELAY_S:
        return reject(f"robots.txt crawl delay {delay:g} s", "robots")
    if web.tdm_reserved(homepage):
        return reject("TDM reservation (tdmrep.json)", "tdm_reserved")

    host = domain_id(homepage)
    discovered: list[str] = []
    # A homepage that fails does not end the check: the proposed feeds may still work
    page_error: Optional[str] = None
    try:
        page = web.get(homepage)
    except RobotsDisallowedError:
        page = None
    except BlockedError as e:
        return reject(f"homepage {e.reason}", "blocked")
    except NotFoundError as e:
        page, page_error = None, f"homepage not found ({e.reason})"
    except FetchError as e:
        page, page_error = None, f"homepage not reachable ({e.reason})"
    if page is not None:
        final_host = domain_id(page.url)
        if final_host != host and final_host in known:
            return reject(f"homepage redirects to {final_host}, which is in the list", "duplicate")
        if web.tdm_reserved(page.url, page):
            return reject("TDM reservation (header)", "tdm_reserved")
        if "html" in page.content_type:
            if parse_page(page.text).tdm_reserved:
                return reject("TDM reservation (meta tag)", "tdm_reserved")
            discovered = feed_links(page.text, page.url)

    urls = sorted(dict.fromkeys([*proposed, *discovered]), key=lambda url: _feed_order(url, outlet))
    recent_after = now - timedelta(days=OUTLET_RECENT_DAYS if outlet else PRESS_RECENT_DAYS)
    feeds: list[str] = []
    duplicates: set[str] = set()
    recent = 0
    readable = False
    # (URL, whether an HTML page at the URL is read for further feed links); overview pages are read one level deep
    queue: deque[tuple[str, bool]] = deque((url, True) for url in urls)
    queued = set(urls)
    checked = 0
    while queue and len(feeds) < MAX_FEEDS_PER_SOURCE and checked < MAX_FEEDS_CHECKED_PER_CANDIDATE:
        if out_of_time():
            return reject("time budget of the review reached before the check ended")
        url, expand = queue.popleft()
        listed = _listed_feed(known_feeds, url)
        if listed is not None:
            duplicates.add(listed)
            continue
        checked += 1
        check = check_feed(web, url, provisional)
        if check.outcome == "blocked":
            return reject(f"feed {check.detail}", "blocked")
        if check.outcome == "invalid" and expand and check.links:
            found = sorted((link for link in check.links if link not in queued),
                           key=lambda link: _feed_order(link, outlet))
            queued.update(found)
            queue.extendleft((link, False) for link in reversed(found))
            continue
        if check.outcome != "ok":
            continue
        listed = _listed_feed(known_feeds, check.moved_to) if check.moved_to else None
        if listed is not None:
            duplicates.add(listed)
            continue
        readable = True
        items = [entry for entry in check.entries if entry.published_at and entry.published_at >= recent_after
                 and looks_sociodemographic(entry)]
        if items:
            feeds.append(check.moved_to or url)
            recent += len(items)
    sitemap: Optional[str] = None
    if outlet:
        sitemap_urls = [candidate.news_sitemap] if candidate.news_sitemap else []
        sitemap_urls.extend(url for url in (rules.sitemaps if rules else ()) if "news" in url.lower())
        for url in dict.fromkeys(sitemap_urls):
            listed = _listed_feed(known_feeds, url)
            if listed is not None:
                duplicates.add(listed)
                continue
            check = check_feed(web, url, provisional, sitemap=True)
            items = [entry for entry in check.entries if entry.published_at and entry.published_at >= recent_after
                     and looks_sociodemographic(entry)]
            readable = readable or check.outcome == "ok"
            if check.outcome == "ok" and items:
                sitemap = url
                recent = max(recent, len(items))
                break
    if not feeds and sitemap is None:
        if duplicates:
            return reject("same feed as " + ", ".join(f"`{listed}`" for listed in sorted(duplicates)), "duplicate")
        if not readable and page_error is not None:
            return reject(f"{page_error}; no readable feed found")
        if not readable:
            return reject("no readable feed found", "no_feed")
        return reject("no feed with recent items", "inactive")
    minimum = MIN_OUTLET_ITEMS if outlet else 1
    if recent < minimum:
        return reject(f"{recent} recent items, fewer than {minimum}", "inactive")

    fetch_pages = not (rules is not None and rules.tdm_statement) and not (delay is not None and
                                                                          delay > MAX_PAGE_CRAWL_DELAY_S)
    days = OUTLET_RECENT_DAYS if outlet else PRESS_RECENT_DAYS
    evidence = [f"{recent} {'sociodemographic ' if provisional.topic_filter else ''}items in the last {days} days"]
    if delay is not None:
        evidence.append(f"robots.txt crawl-delay {delay:g} s")
    if rules is not None and rules.tdm_statement:
        evidence.append("robots.txt states a TDM reservation in natural language, therefore fetch_pages = false")
    comment = f"source review {now.date().isoformat()}: " + "; ".join(evidence)
    source_id = source_id or candidate_id(attributes, host, taken)
    if outlet:
        # The topic filter of an outlet follows from its type (see sources.load_config)
        fields = {"id": source_id, "name": candidate.name, "homepage": homepage,
                  "outlet_type": attributes["outlet_type"], "owner_group": attributes["owner_group"],
                  "home_state": attributes["home_state"], "topic": attributes["topic"],
                  "paywall_model": attributes["paywall_model"], "feeds": feeds, "news_sitemap": sitemap,
                  "fetch_pages": fetch_pages, "added": now.date()}
        return NewEntry("outlet", fields, comment), None
    fields = {"id": source_id, "name": candidate.name, "homepage": homepage, "actor_type": attributes["actor_type"],
              "jurisdiction": attributes["jurisdiction"], "feeds": feeds,
              "topic_filter": True if provisional.topic_filter else None, "fetch_pages": fetch_pages,
              "added": now.date()}
    return NewEntry("press_source", fields, comment), None


def _listed_feed(known_feeds: dict[str, str], url: str) -> Optional[str]:
    """Returns the id of the listed source that has a feed or news sitemap with this URL, if any."""
    try:
        return known_feeds.get(normalize_url(url))
    except ValueError:
        return None


def _source_key(source: Source) -> Optional[str]:
    """Key of the gap that a listed source fills: the statistical office or regional daily of a state, or the
    specialist outlet of a topic; None for other sources. One source per gap is enough."""
    if isinstance(source, PressSource):
        return f"statistical_office:{source.jurisdiction}" if source.actor_type == "statistical_office" else None
    if source.outlet_type == "regional_daily":
        return f"regional_daily:{source.home_state}"
    return f"topic:{source.topic}" if source.topic else None


def _candidate_key(category: SearchCategory, candidate: SearchCandidate) -> Optional[str]:
    """Key of the gap a candidate stands for (see ``_source_key``); None for other candidates."""
    if category == "statistical_offices" and isinstance(candidate, PressCandidate):
        return f"statistical_office:{candidate.jurisdiction}"
    if category == "regional_press" and isinstance(candidate, OutletCandidate):
        return f"regional_daily:{candidate.home_state}"
    if category == "specialist_press" and isinstance(candidate, OutletCandidate):
        return f"topic:{candidate.topic}"
    return None


def _candidate_from_exclusion(entry: ExcludedSource) -> Optional[tuple[SearchCategory, SearchCandidate]]:
    """Rebuilds the search candidate of an exclusion, so that it can be checked again."""
    category: Optional[SearchCategory]
    if entry.kind == "outlet":
        category = {"regional_daily": "regional_press", "specialist_press": "specialist_press"}.get(
            entry.outlet_type or "")
        if category is None:
            return None
        return category, OutletCandidate(name=entry.name, homepage=entry.homepage, feeds=list(entry.feeds),
                                         home_state=entry.home_state, topic=entry.topic,
                                         owner_group=entry.owner_group, paywall_model=entry.paywall_model)
    if entry.kind != "press_source":
        return None
    if entry.actor_type == "statistical_office" and entry.jurisdiction not in (None, "DE"):
        category = "statistical_offices"
    elif entry.actor_type in FEDERAL_ACTOR_TYPES:
        category = "federal"
    elif entry.actor_type in ASSOCIATION_ACTOR_TYPES:
        category = "associations"
    else:
        return None
    return category, PressCandidate(name=entry.name, homepage=entry.homepage, feeds=list(entry.feeds),
                                    actor_type=entry.actor_type, jurisdiction=entry.jurisdiction)


def exclusion_entry(rejection: Rejection, exclusion_id: str, today: date) -> NewEntry:
    """Builds the ``[[excluded]]`` entry of a rejected candidate, with the attributes for its recheck.

    Args:
        rejection: The rejection.
        exclusion_id: Id of the entry.
        today: Day of the review.

    Returns:
        NewEntry: The entry.
    """
    fields: dict[str, Any] = {"id": exclusion_id, "name": rejection.name, "homepage": rejection.homepage}
    if rejection.kind is not None:
        fields["kind"] = rejection.kind
        fields.update(rejection.attributes)
        if rejection.feeds:
            fields["feeds"] = list(rejection.feeds)
    fields.update(status=rejection.status, reason=rejection.reason, checked=today)
    return NewEntry("excluded", fields)


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

def run_press_review(settings: Settings, config_path: Path, web: WebClient, model: Optional[Any], now: datetime,
                     clock: Callable[[], float] = time.monotonic,
                     source_ids: Optional[set[str]] = None) -> tuple[str, ReviewResult]:
    """Reviews the source list and returns the changed configuration.

    Args:
        settings: The run settings (data and state folders).
        config_path: The configuration file.
        web: The HTTP client.
        model: A ``SearchModel`` or a compatible test double; None skips the search, but
            not the recheck of the excluded candidates.
        now: Time of the review (UTC).
        clock: Monotonic clock in seconds, for the time budget.
        source_ids: Check only these sources, without gaps, rechecks of the exclusions and search,
            e.g. to verify a new source; None checks all active sources.

    Returns:
        tuple[str, ReviewResult]: The text of the changed configuration and the result.

    Raises:
        ValueError: If the configuration or the changed configuration is invalid, or a selected
            source is not an active source.
    """
    started = clock()
    config = load_config(config_path)
    health = SourceHealthStore.load(settings.state_dir)
    today = now.date()
    counts = count_items(settings.output_dir, today - timedelta(days=REVIEW_WINDOW_DAYS))
    result = ReviewResult(selected=source_ids is not None)
    sources = list(config.active_sources)
    if source_ids is not None:
        unknown = sorted(source_ids - {source.id for source in sources})
        if unknown:
            raise ValueError(f"Not an active source of {config_path.name}: {', '.join(unknown)}")
        sources = [source for source in sources if source.id in source_ids]
    # Hosts that blocked requests or reserved TDM, and sources removed for low yield, are not requested
    requested = [source for source in sources if health.get(source.id).status not in ("blocked", "tdm_reserved")
                 and not lacks_yield(source, counts.get(source.id, 0), today)]
    prefetch_robots(web, (url for source in requested for url in _source_urls(source)))
    for position, source in enumerate(sources):
        if clock() - started > REVIEW_TIME_BUDGET_S:
            result.notes.append(f"Time budget reached: {len(sources) - position} sources not checked")
            break
        logger.info("Checking the press source %s.", source.id)
        changes, notes, summary = review_source(web, source, health.get(source.id), counts.get(source.id, 0), now)
        result.changes.extend(changes)
        result.notes.extend(notes)
        result.checks.append((source.id, summary))
        result.checked_sources += 1
    if source_ids is not None:
        return apply_changes(config_path.read_text(encoding="utf-8"), config, result.changes, today), result

    leaving = frozenset(change.source_id for change in result.changes if change.action == "remove_source")
    result.gaps = find_gaps(config, leaving)
    state = _CandidateState(
        known=known_hosts(config), known_feeds=known_feeds(config),
        taken={entry.id for entry in (*config.sources, *config.excluded)},
        gap_keys={_source_key(source) for source in config.active_sources if source.id not in leaving},
    )
    _recheck_exclusions(web, config, result, state, now, clock, started)
    if model is not None:
        _search(web, model, config, result, state, now, clock, started)
    return apply_changes(config_path.read_text(encoding="utf-8"), config, result.changes, today), result


@dataclass
class _CandidateState:
    """What the recheck of the exclusions and the search share in one review.

    Attributes:
        known: Hosts of the listed sources and exclusions.
        known_feeds: Source id by normalized feed and news sitemap URL.
        taken: Ids in use.
        gap_keys: Gaps that a source fills (see ``_source_key``).
        additions: Sources admitted in this review.
    """
    known: set[str]
    known_feeds: dict[str, str]
    taken: set[str]
    gap_keys: set[Optional[str]]
    additions: int = 0


def _admit(result: ReviewResult, state: _CandidateState, entry: NewEntry, key: Optional[str],
           candidate: SearchCandidate) -> bool:
    """Adds an admitted candidate to the changes, unless the limit of additions is reached.

    Returns:
        bool: True if the candidate was added.
    """
    if state.additions >= MAX_ADDITIONS_PER_REVIEW:
        result.notes.append(f"{candidate.name} ({candidate.homepage}) passes the checks; not added because of the "
                            f"limit of {MAX_ADDITIONS_PER_REVIEW} additions per review")
        return False
    state.additions += 1
    state.gap_keys.add(key)
    sitemap = entry.fields.get("news_sitemap")
    urls = [*entry.fields.get("feeds", []), *([sitemap] if sitemap else [])]
    for url in urls:
        state.known_feeds[normalize_url(url)] = str(entry.fields["id"])
    result.changes.append(Change("add_source", str(entry.fields["id"]), entry.comment or "", entry=entry))
    return True


def _recheck_exclusions(web: WebClient, config: SourceConfig, result: ReviewResult, state: _CandidateState,
                        now: datetime, clock: Callable[[], float], started: float) -> None:
    """Checks the excluded candidates with a temporary reason again, without the search.

    A candidate that passes now is added under the id of its exclusion, and the
    exclusion is dropped. If its reason changed, the exclusion gets the new
    status; an unchanged result leaves the configuration as it is.
    """
    for exclusion in config.excluded:
        if not exclusion.rechecked:
            continue
        rebuilt = _candidate_from_exclusion(exclusion)
        if rebuilt is None:
            continue
        category, candidate = rebuilt
        key = _candidate_key(category, candidate)
        if key is not None and key in state.gap_keys:
            continue
        if clock() - started > REVIEW_TIME_BUDGET_S:
            result.notes.append("Time budget reached: further excluded candidates not checked again")
            return
        result.rechecked += 1
        own_host = domain_id(exclusion.homepage)
        logger.info("Checking the excluded candidate %s again.", exclusion.id)
        entry, rejection = evaluate_candidate(web, category, candidate, result.gaps, now, state.taken,
                                              state.known - {own_host}, state.known_feeds, source_id=exclusion.id,
                                              out_of_time=lambda: clock() - started > REVIEW_TIME_BUDGET_S)
        if entry is not None:
            if _admit(result, state, entry, key, candidate):
                result.changes.append(Change("drop_exclusion", exclusion.id, "admitted"))
            continue
        assert rejection is not None
        if rejection.status is None or rejection.status == exclusion.status:
            continue
        result.changes.append(Change("exclude", exclusion.id, rejection.reason,
                                     entry=exclusion_entry(rejection, exclusion.id, now.date())))


def _search(web: WebClient, model: Any, config: SourceConfig, result: ReviewResult, state: _CandidateState,
            now: datetime, clock: Callable[[], float], started: float) -> None:
    """Searches for candidates, evaluates them and adds the changes to the result."""
    listed_federal = [source.name for source in config.active_sources if isinstance(source, PressSource)
                      and source.actor_type in FEDERAL_ACTOR_TYPES and source.jurisdiction == "DE"]
    listed_associations = [source.name for source in config.active_sources if isinstance(source, PressSource)
                           and source.actor_type in ASSOCIATION_ACTOR_TYPES]
    seen_hosts: set[str] = set()
    for category, candidate in search_candidates(model, result.gaps, state.known, result, listed_federal,
                                                 listed_associations):
        try:
            host = domain_id(candidate.homepage)
        except ValueError:
            result.rejections.append(Rejection(candidate.name, candidate.homepage, "homepage has no host"))
            continue
        if host in state.known or host in seen_hosts:
            continue
        key = _candidate_key(category, candidate)
        if key is not None and key in state.gap_keys:
            continue
        if clock() - started > REVIEW_TIME_BUDGET_S:
            result.notes.append("Time budget reached: further candidates not evaluated")
            break
        seen_hosts.add(host)
        result.candidates += 1
        logger.info("Checking the candidate %s (%s, search %s).", candidate.name, host, category)
        entry, rejection = evaluate_candidate(web, category, candidate, result.gaps, now, state.taken, state.known,
                                              state.known_feeds,
                                              out_of_time=lambda: clock() - started > REVIEW_TIME_BUDGET_S)
        if entry is not None:
            _admit(result, state, entry, key, candidate)
            continue
        assert rejection is not None
        result.rejections.append(rejection)
        if rejection.status is None:
            continue
        exclusion_id = candidate_id(rejection.attributes, host, state.taken) if rejection.attributes \
            else _new_id(host.split(".")[0], state.taken)
        result.changes.append(Change("exclude", exclusion_id, rejection.reason,
                                     entry=exclusion_entry(rejection, exclusion_id, now.date())))


# ---------------------------------------------------------------------------
# Configuration text
# ---------------------------------------------------------------------------

_TABLE_HEADER = re.compile(r"^\[\[(outlet|press_source|excluded)\]\]$")
_ID_LINE = re.compile(r'^id\s*=\s*"([^"]+)"$')


@dataclass(frozen=True)
class _Block:
    """An entry of the configuration text: the header line and the key lines up to ``end`` (exclusive)."""
    table: str
    source_id: str
    header: int
    end: int


def _blocks(lines: Sequence[str]) -> list[_Block]:
    """Finds the entries of the configuration text.

    Raises:
        ValueError: If an entry has no id line.
    """
    blocks = []
    for index, line in enumerate(lines):
        match = _TABLE_HEADER.match(line.strip())
        if not match:
            continue
        end = index + 1
        while end < len(lines) and lines[end].strip() and not lines[end].lstrip().startswith(("#", "[")):
            end += 1
        ids = [found.group(1) for found in (_ID_LINE.match(text.strip()) for text in lines[index + 1:end]) if found]
        if not ids:
            raise ValueError(f"The entry in line {index + 1} has no id")
        blocks.append(_Block(match.group(1), ids[0], index, end))
    return blocks


def toml_value(value: Any) -> str:
    """Formats a value as TOML.

    Args:
        value: A string, bool, date or a list of strings.

    Returns:
        str: The TOML literal.

    Raises:
        TypeError: For other types.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        # JSON string escapes are valid in TOML basic strings
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(f"Cannot write {type(value).__name__} as TOML")


def _render_entry(entry: NewEntry) -> list[str]:
    """Returns the lines of a new entry, preceded by a blank line."""
    lines = ["", *([f"# {entry.comment}"] if entry.comment else []), f"[[{entry.table}]]"]
    lines.extend(f"{key} = {toml_value(value)}" for key, value in entry.fields.items() if value is not None)
    return lines


def apply_changes(text: str, config: SourceConfig, changes: Sequence[Change], today: date) -> str:
    """Writes the changes into the configuration text and validates the result.

    Unchanged entries and all comments stay as they are. A removed source
    keeps its entry and gets ``removed`` and ``removed_reason``; feed and
    page-fetching changes get a comment line above the entry; new sources
    are appended to their table, exclusions to the excluded entries.

    Args:
        text: The configuration text.
        config: The parsed configuration.
        changes: The changes.
        today: Day of the review.

    Returns:
        str: The changed text; equal to ``text`` without changes.

    Raises:
        ValueError: If a change names an unknown entry or the result is not a valid configuration.
    """
    if not changes:
        return text
    lines = text.split("\n")
    blocks = _blocks(lines)
    by_id = {block.source_id: block for block in blocks}
    sources = {source.id: source for source in config.sources}
    comments: dict[int, list[str]] = {}
    values: dict[int, dict[str, Optional[str]]] = {}
    dropped: set[int] = set()
    feeds: dict[str, list[str]] = {}
    appended: dict[str, list[str]] = {"outlet": [], "press_source": [], "excluded": []}
    for change in changes:
        if change.action == "add_source" or (change.action == "exclude" and change.source_id not in by_id):
            assert change.entry is not None
            appended[change.entry.table].extend(_render_entry(change.entry))
            continue
        if change.source_id not in by_id:
            raise ValueError(f"No entry with the id {change.source_id}")
        block = by_id[change.source_id]
        keys = values.setdefault(block.header, {})
        note = comments.setdefault(block.header, [])
        if change.action == "drop_exclusion":
            dropped.add(block.header)
        elif change.action == "exclude":
            assert change.entry is not None
            for key in ("status", "reason", "checked"):
                keys[key] = toml_value(change.entry.fields[key])
        elif change.action == "remove_source":
            keys["removed"] = toml_value(today)
            keys["removed_reason"] = toml_value(change.reason)
        elif change.action in ("remove_feed", "replace_feed"):
            current = feeds.setdefault(change.source_id, list(sources[change.source_id].feeds))
            if change.action == "remove_feed":
                current[:] = [url for url in current if url != change.old]
                note.append(f"# source review {today.isoformat()}: feed {change.old} removed: {change.reason}")
            else:
                current[:] = list(dict.fromkeys(change.new if url == change.old else url for url in current))
                note.append(f"# source review {today.isoformat()}: feed {change.old} moved to {change.new}")
            keys["feeds"] = toml_value(current)
        elif change.action == "remove_sitemap":
            keys["news_sitemap"] = None
            note.append(f"# source review {today.isoformat()}: news sitemap {change.old} removed: {change.reason}")
        elif change.action == "set_fetch_pages":
            keys["fetch_pages"] = "false"
            note.append(f"# source review {today.isoformat()}: fetch_pages = false: {change.reason}")

    last_end = {table: max((block.end for block in blocks if block.table == table), default=None)
                for table in appended}
    inserts: dict[int, list[str]] = {}
    for table, new_lines in appended.items():
        if not new_lines:
            continue
        position = last_end[table]
        if position is None:
            if table != "excluded":
                raise ValueError(f"The configuration has no [[{table}]] entry to append to")
            position = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
            new_lines = ["", "# Candidates that were checked and not admitted.", *new_lines[1:]]
        inserts.setdefault(position, []).extend(new_lines)

    out: list[str] = []
    block_at = {block.header: block for block in blocks}
    index = 0
    while index < len(lines):
        out.extend(inserts.get(index, []))
        block = block_at.get(index)
        if block is None:
            out.append(lines[index])
            index += 1
            continue
        if block.header in dropped:
            # Remove the blank line before the entry as well
            if out and out[-1] == "":
                out.pop()
            index = block.end
            continue
        if block.header in values:
            out.extend(comments.get(block.header, []))
            out.append(lines[index])
            keys = values[block.header]
            written: set[str] = set()
            for line in lines[index + 1:block.end]:
                key = line.split("=", 1)[0].strip()
                if key in keys:
                    written.add(key)
                    if keys[key] is not None:
                        out.append(f"{key} = {keys[key]}")
                    continue
                out.append(line)
            out.extend(f"{key} = {value}" for key, value in keys.items() if key not in written and value is not None)
            index = block.end
            continue
        out.append(lines[index])
        index += 1
    out.extend(inserts.get(len(lines), []))
    changed = "\n".join(out)
    validate_config_text(changed)
    return changed


def validate_config_text(text: str) -> SourceConfig:
    """Parses a configuration text with the rules of ``load_config``.

    Args:
        text: The configuration text.

    Returns:
        SourceConfig: The parsed configuration.

    Raises:
        ValueError: If the text is not a valid configuration.
    """
    # tomllib.TOMLDecodeError is a ValueError, as are the validation errors of load_config
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "sources.toml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _cell(text: Any) -> str:
    """Escapes a value for a Markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def press_summary(result: ReviewResult, config: SourceConfig) -> str:
    """Returns the summary line of the press review for the summary of the report.

    Args:
        result: The review result.
        config: The configuration before the changes.

    Returns:
        str: A Markdown list item.
    """
    counts = Counter(change.action for change in result.changes)
    edits = sum(counts[action] for action in ("remove_feed", "replace_feed", "remove_sitemap", "set_fetch_pages"))
    return (f"- **News and press sources:** {result.checked_sources} of {len(config.active_sources)} checked; "
            f"proposed {counts['remove_source']} removals, {edits} changes of feeds or page fetching, "
            f"{counts['add_source']} additions, {counts['exclude']} exclusions")


def build_press_section(result: ReviewResult, config: SourceConfig, now: datetime, search: bool) -> list[str]:
    """Builds the section of the report on the news outlets and press sources.

    Args:
        result: The review result.
        config: The configuration before the changes.
        now: Time of the review (UTC).
        search: Whether the search was enabled.

    Returns:
        list[str]: The Markdown lines of the section, starting with a blank line.
    """
    names = {source.id: source.name for source in (*config.sources, *config.excluded)}
    by_action: dict[str, list[Change]] = {}
    for change in result.changes:
        by_action.setdefault(change.action, []).append(change)
    removals = by_action.get("remove_source", [])
    edits = [change for change in result.changes
             if change.action in ("remove_feed", "replace_feed", "remove_sitemap", "set_fetch_pages")]
    additions = by_action.get("add_source", [])
    exclusions = by_action.get("exclude", [])
    if result.selected:
        scope = "only the selected sources; gaps, excluded candidates and the search were not checked"
    else:
        scope = (f"{result.rechecked} excluded candidates again"
                 + (f", and {result.candidates} candidates from {len(result.searches)} searches"
                    if search else "; the search for new sources was turned off"))
    lines = [
        "", "## News and press sources", "",
        f"Checked {result.checked_sources} of {len(config.active_sources)} active sources of `config/sources.toml` "
        f"and {scope}. The changes below are written into `config/sources.toml` of this pull request.",
        "",
        f"**Proposed:** {len(removals)} removals, {len(edits)} changes of feeds or page fetching, "
        f"{len(additions)} additions, {len(exclusions)} exclusions of rejected candidates.",
    ]
    if removals:
        lines.extend(["", "### Removals", "",
                      "The entries stay in the configuration with the date of the review as `removed`.", "",
                      "| Source | Reason |", "|---|---|"])
        lines.extend(f"| {_cell(names.get(change.source_id, change.source_id))} (`{change.source_id}`) | "
                     f"{_cell(change.reason)} |" for change in removals)
    if edits:
        lines.extend(["", "### Feeds and page fetching", "", "| Source | Change | Reason |", "|---|---|---|"])
        for change in edits:
            what = {"remove_feed": f"remove feed {change.old}", "replace_feed": f"{change.old} → {change.new}",
                    "remove_sitemap": f"remove news sitemap {change.old}",
                    "set_fetch_pages": "fetch_pages = false"}[change.action]
            lines.append(f"| `{change.source_id}` | {_cell(what)} | {_cell(change.reason)} |")
    if additions:
        lines.extend(["", "### Additions", "", "| Source | Type | Feeds | Checks |", "|---|---|---|---|"])
        for change in additions:
            assert change.entry is not None
            entry = change.entry.fields
            kind = entry.get("actor_type") or entry.get("outlet_type")
            urls = [*entry.get("feeds", []), *([entry["news_sitemap"]] if entry.get("news_sitemap") else [])]
            lines.append(f"| {_cell(entry['name'])} (`{entry['id']}`) | {_cell(kind)} | "
                         f"{_cell(', '.join(urls))} | {_cell(change.reason.split(': ', 1)[-1])} |")
    if exclusions:
        lines.extend(["", "### Exclusions", "",
                      "Rejected candidates are added to the `[[excluded]]` entries, so that the search does not "
                      "propose them again. Every review checks the entries with the status no_feed, inactive or "
                      "robots again and adds them when they pass.", "",
                      "| Candidate | Status | Reason |", "|---|---|---|"])
        for change in exclusions:
            assert change.entry is not None
            entry = change.entry.fields
            lines.append(f"| {_cell(entry['name'])} ({_cell(entry['homepage'])}) | {entry['status']} | "
                         f"{_cell(entry['reason'])} |")
    temporary = [rejection for rejection in result.rejections if rejection.status is None]
    if temporary:
        lines.extend(["", "### Candidates not admitted for now", "",
                      "These candidates are not excluded; the next review may check them again.", "",
                      "| Candidate | Reason |", "|---|---|"])
        lines.extend(f"| {_cell(rejection.name)} ({_cell(rejection.homepage)}) | {_cell(rejection.reason)} |"
                     for rejection in temporary)
    if not result.selected:
        gaps = result.gaps
        lines.extend([
            "", "### Gaps", "",
            "- **States without a press source of their statistical office (searched):** "
            + (", ".join(gaps.statistical_states) or "none"),
            "- **States without a regional daily (searched):** " + (", ".join(gaps.regional_states) or "none"),
            "- **Topics without a specialist outlet (searched):** " + (", ".join(gaps.topics) or "none"),
        ])
    if result.checks:
        lines.extend(["", "<details><summary>Feeds of the checked sources</summary>", "",
                      "| Source | Feeds |", "|---|---|"])
        lines.extend(f"| `{source_id}` | {_cell(summary)} |" for source_id, summary in result.checks)
        lines.extend(["", "</details>"])
    if result.notes:
        lines.extend(["", "### Notes", ""])
        lines.extend(f"- {_cell(note)}" for note in result.notes[:MAX_REPORT_NOTES])
        if len(result.notes) > MAX_REPORT_NOTES:
            lines.append(f"- {len(result.notes) - MAX_REPORT_NOTES} further notes are in the workflow log")
    return lines
