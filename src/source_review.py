"""
Monthly review of the data sources: the catalog entries, the regions and the terms of the providers.

The review
1. fetches every active catalog entry from its provider for the last few
   periods and reports entries without data, failing keys, regions or levels
   without data and series whose newest period is older than expected for
   their frequency; for an entry without data or with a failing key it
   probes the dataset (dimensions and available codes for Germany), so that
   the key can be corrected;
2. reads the table of contents of Eurostat: datasets of the catalog that are
   no longer listed (discontinued or renamed, with possible successors) and
   new or restructured datasets on the topics of the catalog (candidates);
3. compares the German NUTS codes that Eurostat publishes with the region
   registry and lists codes that are not registered (a new version of the
   NUTS classification);
4. reads the state and the run log: catalog entries without stored series,
   stale stored series and entries that failed in recent runs;
5. fetches the terms of use of every provider and compares the hash of their
   text with ``terms_hash`` in ``config/providers.toml``.

The review writes a Markdown report and, when the text of a terms page
changed, the new hash into ``config/providers.toml``. The workflow
``source_review.yml`` stores the report as ``reviews/YYYY-MM.md`` and opens a
pull request with it. Changes of the catalog and the region registry are
proposed in the report only, because they need a verified key and a
measurement log entry (CLAUDE.md, section 9). Every request goes through
``WebClient`` and its fetch rules; the terms pages are website requests that
respect robots.txt.

Usage:
    uv run python src/source_review.py --report PATH [--concepts NAME,...] [--no-terms]
                                       [--providers PATH] [--output-dir PATH]
    uv run python src/source_review.py --report PATH --probe "URL URL ..." [--link-filter REGEX]
"""
import argparse
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from catalog import CatalogEntry, CatalogError, active_entries
from collectors.sdmx import (
    PROVIDERS,
    REQUEST_TIMEOUT_S,
    SdmxProvider,
    TocEntry,
    csv_dimensions,
    fetch_entry,
    fetch_eurostat_toc,
)
from collectors.series import DATASET as SERIES_DATASET
from collectors.series import SeriesKey
from probe import probe_url
from providers import CONFIG_PATH as PROVIDERS_PATH
from providers import Provider, ProvidersError, load_providers, set_terms_hash
from regions import Region, RegistryError, active_regions, load_regions
from settings import Settings
from state import RUN_LOG_FILE, SeenStore
from web import FetchError, NotFoundError, WebClient

logger = logging.getLogger(__name__)

REVIEW_WINDOW_DAYS = 35
# Years before the current one that the live check fetches
CHECK_YEARS = 3
# A series whose newest period ended longer ago than this many days is stale
STALE_DAYS: dict[str, int] = {"W": 180, "M": 120, "Q": 270, "A": 1100}
# Prefixes of Eurostat dataset codes on the topics of the catalog; new datasets with them are candidates
WATCHED_PREFIXES = ("demo_", "migr_", "edat_", "educ_", "trng_", "ilc_", "lfst_r_", "lfst_hh", "hlth_", "yth_",
                    "nama_10r_", "cens_", "proj_", "hbs_", "icw_", "tus_", "urt_", "met_")
MAX_CANDIDATES = 30
MAX_SUCCESSORS = 3
MAX_CODES_SHOWN = 30
MAX_REPORT_NOTES = 40
EUROSTAT_DSD_URL = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/datastructure/ESTAT/{flow}"
_DIMENSION_TAG = re.compile(r"<(?:[\w-]+:)?Dimension\b([^>]*)>")
_ATTRIBUTE = re.compile(r'\b(id|position)="([^"]*)"')
_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class EntryCheck:
    """Result of the live check of one catalog entry.

    Attributes:
        concept (str): The catalog entry.
        provider (str): Its provider.
        flow (str): Its dataset or dataflow.
        status (str): ok, attention (data with gaps or stale), no_data or failed.
        series (int): Series with values in the checked periods.
        expected (int): Series the entry can have.
        coverage (dict[str, tuple[int, int]]): Areas with data and requested areas per level.
        newest (Optional[str]): Newest period of all series.
        example (Optional[str]): Newest value of the first series with values, as a check of the magnitude.
        problems (list[str]): Descriptions of the problems.
        probe (list[str]): Dimensions and codes of the dataset for an entry without data or with a failing key.
    """
    concept: str
    provider: str
    flow: str
    status: str = "ok"
    series: int = 0
    expected: int = 0
    coverage: dict[str, tuple[int, int]] = field(default_factory=dict)
    newest: Optional[str] = None
    example: Optional[str] = None
    problems: list[str] = field(default_factory=list)
    probe: list[str] = field(default_factory=list)


@dataclass
class TermsCheck:
    """Result of the check of the terms page of a provider.

    Attributes:
        provider (str): Provider id.
        url (str): The terms page.
        status (str): unchanged, changed, recorded (first hash) or unavailable.
        new_hash (Optional[str]): Hash of the current text.
        detail (str): Error or remark.
    """
    provider: str
    url: str
    status: str
    new_hash: Optional[str] = None
    detail: str = ""


@dataclass
class ReviewResult:
    """Everything the review found.

    Attributes:
        checks (list[EntryCheck]): Live checks of the catalog entries.
        toc_available (bool): Whether the Eurostat table of contents could be read.
        not_listed (list[tuple[str, str, list[str]]]): Concept, dataset and possible successors of catalog
            datasets missing from the table of contents.
        candidates (list[TocEntry]): New or restructured datasets on the topics of the catalog.
        unregistered_regions (list[str]): German NUTS codes at Eurostat that are not in the registry.
        regions_checked_with (Optional[str]): Dataset used for the region check.
        unstored_entries (list[str]): Catalog entries without stored series.
        stale_series (list[str]): Entries with stored series whose newest period is old.
        run_failures (Counter): Failures per concept in the runs of the review window.
        terms (list[TermsCheck]): Checks of the terms pages.
        notes (list[str]): Further remarks.
    """
    checks: list[EntryCheck] = field(default_factory=list)
    toc_available: bool = False
    not_listed: list[tuple[str, str, list[str]]] = field(default_factory=list)
    candidates: list[TocEntry] = field(default_factory=list)
    unregistered_regions: list[str] = field(default_factory=list)
    regions_checked_with: Optional[str] = None
    unstored_entries: list[str] = field(default_factory=list)
    stale_series: list[str] = field(default_factory=list)
    run_failures: Counter[str] = field(default_factory=Counter)
    terms: list[TermsCheck] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

def period_end(period: str, frequency: str) -> Optional[date]:
    """Returns the last day of a stored period.

    Args:
        period: The period, e.g. 2026-09, 2026-Q3, 2026-W38 or 2026.
        frequency: Its frequency (W, M, Q, A).

    Returns:
        Optional[date]: The last day; None if the period cannot be read.
    """
    try:
        year = int(period[:4])
        if frequency == "W":
            return date.fromisocalendar(year, int(period[6:]), 7)
        if frequency == "M":
            month = int(period[5:7])
            return date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
        if frequency == "Q":
            quarter = int(period[6])
            return date(year + quarter // 4, (quarter * 3) % 12 + 1, 1) - timedelta(days=1)
        return date(year, 12, 31)
    except (ValueError, IndexError):
        return None


def is_stale(period: Optional[str], frequency: str, today: date) -> bool:
    """Tells whether the newest period of a series is older than expected for its frequency.

    Args:
        period: The newest period; None counts as stale.
        frequency: Frequency of the series.
        today: Day of the review.

    Returns:
        bool: True if the period ended more than STALE_DAYS ago.
    """
    end = period_end(period, frequency) if period else None
    return end is None or (today - end).days > STALE_DAYS[frequency]


# ---------------------------------------------------------------------------
# Live checks of the catalog entries
# ---------------------------------------------------------------------------

def csv_codes(text: str) -> tuple[list[str], dict[str, set[str]]]:
    """Reads the dimension columns of an SDMX-CSV response and the codes that occur in them.

    Args:
        text: The CSV text.

    Returns:
        tuple: The dimension columns in order, and the codes per column.

    Raises:
        ValueError: If the response is not an SDMX-CSV.
    """
    dimensions, rows = csv_dimensions(text)
    codes: dict[str, set[str]] = {name: set() for name in dimensions}
    for row in rows:
        for name in dimensions:
            codes[name].add(row.get(name) or "")
    return dimensions, codes


def eurostat_dimensions(web: WebClient, flow: str) -> list[str]:
    """Reads the dimensions of a Eurostat dataset from its data structure definition.

    Args:
        web: The HTTP client.
        flow: The dataset code.

    Returns:
        list[str]: Dimension ids in key order, without the time dimension.

    Raises:
        FetchError: If the request fails.
        ValueError: If the response has no dimensions.
    """
    response = web.get(EUROSTAT_DSD_URL.format(flow=flow.upper()), respect_robots=False, timeout_s=REQUEST_TIMEOUT_S)
    found: list[tuple[int, str]] = []
    for number, match in enumerate(_DIMENSION_TAG.finditer(response.text)):
        attributes = dict(_ATTRIBUTE.findall(match.group(1)))
        if "id" in attributes:
            position = attributes.get("position", "")
            found.append((int(position) if position.isdigit() else number, attributes["id"]))
    if not found:
        raise ValueError(f"no dimensions in the structure of {flow}")
    return [dimension for _position, dimension in sorted(found)]


def _code_list(values: Sequence[str]) -> str:
    """Formats codes for the report, at most MAX_CODES_SHOWN of them."""
    shown = ", ".join(values[:MAX_CODES_SHOWN])
    return shown + (f" and {len(values) - MAX_CODES_SHOWN} more" if len(values) > MAX_CODES_SHOWN else "")


def _probe_get(web: WebClient, provider: SdmxProvider, flow: str, key: str, today: date) -> tuple[list[str], dict[str, set[str]]]:
    """Requests the recent periods of a key of a dataset and returns its dimension columns and codes."""
    params = {**dict(provider.params), "startPeriod": str(today.year - CHECK_YEARS)}
    response = web.get(provider.url(flow, key), params=params, headers=dict(provider.headers) or None,
                       respect_robots=False, timeout_s=REQUEST_TIMEOUT_S)
    return csv_codes(response.text)


def probe_entry(web: WebClient, provider: SdmxProvider, entry: CatalogEntry, geo: str, today: date) -> list[str]:
    """Describes the dataset of an entry so that a failing key can be corrected.

    If the number of dimensions of the dataset equals the number of parts of the key template,
    one request per dimension leaves that dimension open and keeps the requested codes of the
    others; the report lists the codes of the dimension and the requested codes the dataset
    lacks. Otherwise one request with every dimension open except the area lists the codes of
    all dimensions.

    Args:
        web: The HTTP client.
        provider: The SDMX interface.
        entry: The catalog entry.
        geo: Area of the probe requests, e.g. DE.
        today: Day of the review.

    Returns:
        list[str]: Lines for the report.
    """
    lines: list[str] = []
    dimensions: Optional[list[str]] = None
    if provider.name == "eurostat":
        try:
            dimensions = eurostat_dimensions(web, entry.flow)
            lines.append(f"structure: {len(dimensions)} dimensions ({', '.join(dimensions)}); "
                         f"the key template {entry.key} has {len(entry.key_parts)} parts")
        except (FetchError, ValueError) as e:
            lines.append(f"structure not available: {e}")
    positions = entry.placeholder_positions()
    breakdown = dict(entry.breakdown)
    if dimensions is not None and len(dimensions) != len(entry.key_parts):
        geo_position = dimensions.index("geo") if "geo" in dimensions else len(dimensions) - 1
        key = ".".join(geo if position == geo_position else "" for position in range(len(dimensions)))
        try:
            columns, codes = _probe_get(web, provider, entry.flow, key, today)
        except (FetchError, ValueError) as e:
            lines.append(f"no data for {geo} with the key {key}: {e}")
            return lines
        lines.extend(f"{column}: {_code_list(sorted(codes[column]))}" for column in columns)
        return lines
    placeholders = {position: name for name, position in positions.items()}
    requested: list[list[str]] = []
    for position, part in enumerate(entry.key_parts):
        name = placeholders.get(position)
        requested.append([geo] if name == "geo" else list(breakdown[name]) if name else [part])
    for position, wanted in enumerate(requested):
        if position == positions["geo"]:
            continue
        label = dimensions[position] if dimensions else f"part {position + 1}"
        key = ".".join("" if index == position else "+".join(codes) for index, codes in enumerate(requested))
        try:
            columns, codes = _probe_get(web, provider, entry.flow, key, today)
        except (FetchError, ValueError) as e:
            lines.append(f"{label}: no data with the other parts as requested ({str(e)[:120]})")
            continue
        found = sorted(codes[columns[position]]) if position < len(columns) else []
        missing = [code for code in wanted if code not in found]
        lines.append(f"{label}: requested {_code_list(wanted)}; missing {_code_list(missing) or 'none'}; "
                     f"available {_code_list(found)}")
    return lines


def check_entry(web: WebClient, entry: CatalogEntry, regions: Sequence[Region], today: date) -> EntryCheck:
    """Fetches the recent periods of a catalog entry and describes its coverage.

    Args:
        web: The HTTP client.
        entry: The catalog entry.
        regions: The active regions.
        today: Day of the review.

    Returns:
        EntryCheck: The result.
    """
    provider = PROVIDERS[entry.provider]
    geos = entry.geos(regions)
    probe_geo = "DE" if "DE" in geos else geos[0]
    result = EntryCheck(concept=entry.concept, provider=entry.provider, flow=entry.flow,
                        expected=entry.expected_series(regions))
    try:
        data = fetch_entry(web, provider, entry, geos, start_period=str(today.year - CHECK_YEARS))
    except NotFoundError as e:
        result.status = "no_data"
        result.problems.append(f"no data for the key: {e.reason}")
        result.probe = probe_entry(web, provider, entry, probe_geo, today)
        return result
    except (FetchError, ValueError) as e:
        result.status = "failed"
        result.problems.append(str(e)[:300])
        result.probe = probe_entry(web, provider, entry, probe_geo, today)
        return result
    result.series = len(data.series)
    levels = {region.geo: region.level for region in regions}
    found_geos = {key.geo for key in data.series}
    for geo in geos:
        level = levels.get(geo, "eu")
        found, requested = result.coverage.get(level, (0, 0))
        result.coverage[level] = (found + (geo in found_geos), requested + 1)
    empty_levels = [level for level, (found, _requested) in result.coverage.items() if not found]
    if empty_levels:
        result.problems.append(f"no data for the levels {', '.join(empty_levels)}")
    periods = [period for series in data.series.values() for period in series.values]
    result.newest = max(periods) if periods else None
    for key in sorted(data.series, key=lambda item: item.series_id):
        values = data.series[key].values
        if values:
            period = max(values)
            result.example = f"{key.series_id} {period}: {values[period]:,}"
            break
    if is_stale(result.newest, entry.frequency, today):
        result.problems.append(f"newest period {result.newest or 'none'} is older than expected")
    if result.series < result.expected:
        missing = result.expected - result.series
        result.problems.append(f"{missing} of {result.expected} series without values since {today.year - CHECK_YEARS}")
    if data.unknown_geos:
        result.problems.append(f"the dataset does not know {len(data.unknown_geos)} areas: "
                               + ", ".join(data.unknown_geos[:MAX_CODES_SHOWN]))
    if result.problems:
        result.status = "attention"
    return result


def unregistered_regions(web: WebClient, catalog: Sequence[CatalogEntry], registered: set[str],
                         today: date) -> tuple[Optional[str], list[str]]:
    """Lists the German NUTS codes that Eurostat publishes in a district dataset and the registry lacks.

    Args:
        web: The HTTP client.
        catalog: The active catalog entries.
        registered: All codes of the registry, including removed ones.
        today: Day of the review.

    Returns:
        tuple: The concept used for the check (None if no entry has districts), and the unregistered codes.

    Raises:
        FetchError: If the request fails.
        ValueError: If the response is not an SDMX-CSV.
    """
    entry = next((item for item in catalog if item.provider == "eurostat" and "nuts3" in item.geo_levels), None)
    if entry is None:
        return None, []
    key = entry.fill_key("", {name: codes[0] for name, codes in entry.breakdown})
    params = {**dict(PROVIDERS["eurostat"].params), "startPeriod": str(today.year - CHECK_YEARS)}
    response = web.get(PROVIDERS["eurostat"].url(entry.flow, key), params=params, respect_robots=False,
                       timeout_s=REQUEST_TIMEOUT_S)
    columns, codes = csv_codes(response.text)
    geo_column = columns[entry.placeholder_positions()["geo"]] if len(columns) == len(entry.key_parts) else "geo"
    german = {code for code in codes.get(geo_column, set()) if code.startswith("DE") and not code.endswith("Z")}
    return entry.concept, sorted(german - registered)


# ---------------------------------------------------------------------------
# Table of contents
# ---------------------------------------------------------------------------

def _words(text: str) -> set[str]:
    """Returns the lower-case words of a text."""
    return set(_WORD.findall(text.lower()))


def possible_successors(entry: CatalogEntry, toc: dict[str, TocEntry]) -> list[str]:
    """Finds datasets of the table of contents that may have replaced a dataset of the catalog.

    Candidates share the first part of the code (e.g. ``demo``) and are ranked by the overlap of
    their title with the name of the catalog entry.

    Args:
        entry: The catalog entry whose dataset is not listed.
        toc: The table of contents.

    Returns:
        list[str]: Up to MAX_SUCCESSORS descriptions ``code (title)``.
    """
    stem = entry.flow.split("_", 1)[0] + "_"
    name_words = _words(entry.name)
    scored = []
    for item in toc.values():
        if not item.code.startswith(stem):
            continue
        title_words = _words(item.title)
        overlap = len(name_words & title_words) / max(1, len(name_words | title_words))
        if overlap > 0:
            scored.append((overlap, item.code, item.title))
    scored.sort(key=lambda value: (-value[0], value[1]))
    return [f"{code} ({title})" for _overlap, code, title in scored[:MAX_SUCCESSORS]]


def candidate_datasets(toc: dict[str, TocEntry], catalog_flows: set[str], today: date) -> list[TocEntry]:
    """Lists datasets on the topics of the catalog that are new or were restructured recently.

    Args:
        toc: The table of contents.
        catalog_flows: Datasets of the catalog.
        today: Day of the review.

    Returns:
        list[TocEntry]: Up to MAX_CANDIDATES datasets, regional datasets first, newest change first.
    """
    cutoff = today - timedelta(days=REVIEW_WINDOW_DAYS)
    found = [item for item in toc.values()
             if item.code.startswith(WATCHED_PREFIXES) and item.code not in catalog_flows
             and item.structure_change is not None and item.structure_change >= cutoff]
    found.sort(key=lambda item: ("nuts" not in item.title.lower() and "region" not in item.title.lower(),
                                 -item.structure_change.toordinal() if item.structure_change else 0, item.code))
    return found[:MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# State and run log
# ---------------------------------------------------------------------------

def stored_coverage(state_dir: Path, catalog: Sequence[CatalogEntry], regions: Sequence[Region],
                    today: date) -> tuple[list[str], list[str]]:
    """Finds catalog entries without stored series and entries whose stored series are stale.

    Args:
        state_dir: Folder of the state files.
        catalog: The active entries.
        regions: The active regions.
        today: Day of the review.

    Returns:
        tuple: Entries without stored series, and descriptions of entries with stale series.
    """
    unstored: list[str] = []
    stale: list[str] = []
    for entry in catalog:
        shards = {SeriesKey(entry, geo).shard for geo in entry.geos(regions)}
        newest: dict[str, str] = {}
        for shard in shards:
            store = SeenStore.load(state_dir, SERIES_DATASET, shard)
            for series_id in store.keys():
                stored = store.get(series_id)
                periods = sorted((stored.data.get("values") or {}) if stored else {})
                if periods:
                    newest[series_id] = periods[-1]
        if not newest:
            unstored.append(entry.concept)
            continue
        old = sorted(series_id for series_id, period in newest.items() if is_stale(period, entry.frequency, today))
        if old:
            stale.append(f"`{entry.concept}`: {len(old)} of {len(newest)} series end before the expected period, "
                         f"e.g. `{old[0]}` ({newest[old[0]]})")
    return unstored, stale


def recent_run_failures(state_dir: Path, since: datetime) -> Counter[str]:
    """Counts the runs in which a catalog entry had no data or failed.

    Args:
        state_dir: Folder of the state files.
        since: Runs that started earlier are ignored.

    Returns:
        Counter: Number of runs per concept.
    """
    path = state_dir / RUN_LOG_FILE
    failures: Counter[str] = Counter()
    if not path.exists():
        return failures
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            started = datetime.fromisoformat(entry["started"])
        except (ValueError, KeyError):
            continue
        if started < since:
            continue
        for outcome in entry.get("collectors", {}).values():
            counters = outcome.get("counters", {})
            failures.update(counters.get("not_found_entries", []))
            failures.update(text.split(":", 1)[0] for text in counters.get("failed_entries", []))
    return failures


# ---------------------------------------------------------------------------
# Terms of use
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Collects the visible text of an HTML page, without scripts and styles."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def page_text(html: str) -> str:
    """Returns the visible text of an HTML page with normalized whitespace.

    Args:
        html: The page.

    Returns:
        str: The text.
    """
    extractor = _TextExtractor()
    extractor.feed(html)
    return " ".join(" ".join(extractor.parts).split())


def check_terms(web: WebClient, provider: Provider) -> TermsCheck:
    """Fetches the terms page of a provider and compares the hash of its text with the configuration.

    Args:
        web: The HTTP client.
        provider: The provider.

    Returns:
        TermsCheck: The result.
    """
    try:
        response = web.get(provider.terms_url)
    except FetchError as e:
        return TermsCheck(provider.id, provider.terms_url, "unavailable", detail=str(e)[:200])
    text = page_text(response.text)
    if not text:
        return TermsCheck(provider.id, provider.terms_url, "unavailable", detail="the page has no text")
    new_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if provider.terms_hash is None:
        return TermsCheck(provider.id, provider.terms_url, "recorded", new_hash, f"{len(text):,} characters")
    if new_hash == provider.terms_hash:
        return TermsCheck(provider.id, provider.terms_url, "unchanged", new_hash)
    return TermsCheck(provider.id, provider.terms_url, "changed", new_hash,
                      f"the text differs from the version of {provider.terms_hash_since or 'the last review'}")


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

def run_review(settings: Settings, web: WebClient, now: datetime, concepts: Optional[set[str]] = None,
               providers: Optional[list[Provider]] = None) -> ReviewResult:
    """Runs all checks of the review.

    Args:
        settings: The settings (output folder with the state).
        web: The HTTP client.
        now: Time of the review (UTC).
        concepts: Catalog entries to check; None checks all active entries.
        providers: Providers whose terms are checked; None skips the terms.

    Returns:
        ReviewResult: The findings.
    """
    today = now.date()
    result = ReviewResult()
    catalog = active_entries()
    regions = active_regions()
    selected = [entry for entry in catalog if concepts is None or entry.concept in concepts]
    unknown = sorted((concepts or set()) - {entry.concept for entry in catalog})
    if unknown:
        result.notes.append(f"unknown concepts: {', '.join(unknown)}")
    toc: dict[str, TocEntry] = {}
    try:
        toc = fetch_eurostat_toc(web)
        result.toc_available = True
    except (FetchError, ValueError) as e:
        result.notes.append(f"Eurostat table of contents not available: {e}")
    for entry in selected:
        logger.info("Checking %s (%s %s).", entry.concept, entry.provider, entry.flow)
        check = check_entry(web, entry, regions, today)
        result.checks.append(check)
        if toc and entry.provider == "eurostat" and entry.flow not in toc:
            result.not_listed.append((entry.concept, entry.flow, possible_successors(entry, toc)))
    if toc:
        result.candidates = candidate_datasets(toc, {entry.flow for entry in catalog}, today)
    try:
        registered = {region.geo for region in load_regions()}
        result.regions_checked_with, result.unregistered_regions = unregistered_regions(web, catalog, registered,
                                                                                       today)
    except (FetchError, ValueError) as e:
        result.notes.append(f"region check failed: {e}")
    result.unstored_entries, result.stale_series = stored_coverage(settings.state_dir, selected, regions, today)
    result.run_failures = recent_run_failures(settings.state_dir, now - timedelta(days=REVIEW_WINDOW_DAYS))
    for provider in providers or []:
        result.terms.append(check_terms(web, provider))
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _cell(text: Any) -> str:
    """Escapes a value for a Markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def build_report(result: ReviewResult, now: datetime, catalog_size: int) -> str:
    """Builds the Markdown report of a review, used as review file and pull request description.

    Args:
        result: The review result.
        now: Time of the review (UTC).
        catalog_size: Number of active catalog entries.

    Returns:
        str: The Markdown text.
    """
    statuses = Counter(check.status for check in result.checks)
    attention = [check for check in result.checks if check.status != "ok"]
    changed_terms = [check for check in result.terms if check.status in ("changed", "recorded")]
    lines = [
        f"# Source review {now:%Y-%m}",
        "",
        f"Review of {now:%Y-%m-%d %H:%M} UTC. Checked {len(result.checks)} of {catalog_size} catalog entries live "
        f"(periods since {now.year - CHECK_YEARS}), the Eurostat table of contents "
        f"({'read' if result.toc_available else 'not available'}), the German NUTS codes and the terms of "
        f"{len(result.terms)} providers.",
        "",
        "## Table of Contents",
        "- [Summary](#summary)",
        "- [Entries that need attention](#entries-that-need-attention)",
        "- [Coverage of the catalog entries](#coverage-of-the-catalog-entries)",
        "- [Datasets and regions](#datasets-and-regions)",
        "- [Stored series and recent runs](#stored-series-and-recent-runs)",
        "- [Terms of use](#terms-of-use)",
        "- [Next steps](#next-steps)",
        "",
        "## Summary",
        "",
        f"- **Catalog entries:** {statuses.get('ok', 0)} ok, {statuses.get('attention', 0)} with gaps or old "
        f"periods, {statuses.get('no_data', 0)} without data, {statuses.get('failed', 0)} failed",
        f"- **Datasets no longer listed by Eurostat:** {len(result.not_listed)}",
        f"- **Candidate datasets:** {len(result.candidates)}",
        f"- **Unregistered German NUTS codes:** {len(result.unregistered_regions)}",
        f"- **Terms pages changed or recorded:** {len(changed_terms)}",
        "",
        "## Entries that need attention",
        "",
    ]
    if attention:
        lines.extend(["| Concept | Dataset | Status | Problems |", "|---|---|---|---|"])
        lines.extend(f"| `{check.concept}` | {check.provider} `{check.flow}` | {check.status} | "
                     f"{_cell('; '.join(check.problems))} |" for check in attention)
        probes = [check for check in attention if check.probe]
        if probes:
            lines.extend(["", "Structure and codes of the datasets of entries without data or with a failing key "
                              "(codes for DE in the checked periods):", ""])
            for check in probes:
                lines.extend([f"<details><summary><code>{check.concept}</code> ({check.flow})</summary>", "", "```text",
                              *check.probe, "```", "", "</details>", ""])
    else:
        lines.append("None.")
    lines.extend(["", "## Coverage of the catalog entries", "",
                  "<details><summary>Series with values and areas with data per level</summary>", "",
                  "| Concept | Status | Series | Newest period | Areas with data | Example value |",
                  "|---|---|---|---|---|---|"])
    for check in result.checks:
        coverage = ", ".join(f"{level} {found}/{requested}" for level, (found, requested) in check.coverage.items())
        lines.append(f"| `{check.concept}` | {check.status} | {check.series:,} of {check.expected:,} | "
                     f"{check.newest or 'none'} | {coverage or 'none'} | {_cell(check.example or 'none')} |")
    lines.extend(["", "</details>", "", "## Datasets and regions", ""])
    if result.not_listed:
        lines.extend(["**Catalog datasets that the table of contents of Eurostat no longer lists** (discontinued or "
                      "renamed):", "", "| Concept | Dataset | Possible successors |", "|---|---|---|"])
        lines.extend(f"| `{concept}` | `{flow}` | {_cell('; '.join(successors) or 'none found')} |"
                     for concept, flow, successors in result.not_listed)
        lines.append("")
    if result.candidates:
        lines.extend([f"**Candidate datasets:** datasets on the topics of the catalog that are new or whose structure "
                      f"changed in the last {REVIEW_WINDOW_DAYS} days.", "",
                      "| Dataset | Title | Structure changed | Data |", "|---|---|---|---|"])
        lines.extend(f"| `{item.code}` | {_cell(item.title)} | {item.structure_change} | "
                     f"{_cell(item.data_start)} to {_cell(item.data_end)} |" for item in result.candidates)
        lines.append("")
    if result.regions_checked_with:
        if result.unregistered_regions:
            lines.append(f"**German NUTS codes not in `config/regions.toml`** (dataset of `{result.regions_checked_with}`): "
                         + ", ".join(f"`{code}`" for code in result.unregistered_regions)
                         + ". A new version of the NUTS classification may be in use; add the codes with their "
                           "names and versions.")
        else:
            lines.append(f"All German NUTS codes of the dataset of `{result.regions_checked_with}` are registered.")
    if not (result.not_listed or result.candidates or result.regions_checked_with):
        lines.append("Nothing to report.")
    lines.extend(["", "## Stored series and recent runs", ""])
    if result.unstored_entries:
        lines.append("- **Entries without stored series:** " + ", ".join(f"`{name}`" for name in result.unstored_entries))
    lines.extend(f"- {note}" for note in result.stale_series[:MAX_REPORT_NOTES])
    if result.run_failures:
        lines.append(f"- **Entries without data or failed in runs of the last {REVIEW_WINDOW_DAYS} days:** "
                     + ", ".join(f"`{name}` ({count})" for name, count in result.run_failures.most_common()))
    if not (result.unstored_entries or result.stale_series or result.run_failures):
        lines.append("Nothing to report.")
    lines.extend(["", "## Terms of use", "", "| Provider | Page | Status | Detail |", "|---|---|---|---|"])
    lines.extend(f"| {check.provider} | {check.url} | {check.status} | {_cell(check.detail)} |" for check in result.terms)
    if not result.terms:
        lines.append("| none checked | | | |")
    if result.notes:
        lines.extend(["", "**Notes**", ""])
        lines.extend(f"- {_cell(note)}" for note in result.notes[:MAX_REPORT_NOTES])
    lines.extend([
        "", "## Next steps", "",
        "- Correct or remove catalog entries without data in `config/series.toml`; verify a new key against the live "
        "provider, keep removed entries with `removed` and `removed_reason`, and record every change in the "
        "measurement log of `project_architecture.md`.",
        "- Add candidate datasets that extend the catalog, with verified keys.",
        "- Add unregistered NUTS codes to `config/regions.toml`.",
        "- Read the terms pages marked as changed; the pull request records their new hash in "
        "`config/providers.toml`.",
        "- Merge this pull request to keep the report in `reviews/`. The next review replaces the branch, so merge "
        "or close the pull request before then.",
    ])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_probes(urls: Sequence[str], link_filter: Optional[str], report: Path, web: Optional[WebClient] = None) -> int:
    """Checks URLs with the fetch rules and writes their description as a Markdown report.

    Args:
        urls: The URLs.
        link_filter: Regular expression for the links of HTML pages to list.
        report: Path of the report.
        web: HTTP client; created if omitted.

    Returns:
        int: 0.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    lines = [f"# Source probe {now:%Y-%m-%d %H:%M} UTC", "",
             f"Checked {len(urls)} URLs with the fetch rules of the pipeline"
             + (f"; links filtered with `{link_filter}`" if link_filter else "") + ".", ""]
    client = web or WebClient()
    try:
        for url in urls:
            logger.info("Probing %s.", url)
            lines.extend([f"## {url}", "", "```text", *probe_url(client, url, link_filter), "```", ""])
    finally:
        if web is None:
            client.close()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return 0


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    """Parses the command-line arguments.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        argparse.Namespace: The parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Reviews the data sources and writes a report.")
    parser.add_argument("--report", type=Path, required=True, help="path of the Markdown report to write")
    parser.add_argument("--concepts", help="comma-separated catalog entries to check (default: all active entries)")
    parser.add_argument("--terms", action=argparse.BooleanOptionalAction, default=True,
                        help="check the terms pages of the providers (default: on)")
    parser.add_argument("--providers", type=Path, default=PROVIDERS_PATH, help="the provider configuration")
    parser.add_argument("--probe", help="URLs separated by spaces: check them with the fetch rules and describe "
                                         "their content instead of running the review")
    parser.add_argument("--link-filter", help="regular expression for the links of probed HTML pages to list")
    parser.add_argument("--output-dir", type=Path,
                        help="folder with the datasets and state (default: data/ in GitHub Actions, "
                             "~/Downloads/german_sociodemographics_dataset locally)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Runs the review and writes the report and changed terms hashes.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        int: 0 on success, 1 if the settings or the configuration are invalid.
    """
    args = parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.probe:
        return run_probes(args.probe.split(), args.link_filter, args.report)
    try:
        settings = Settings.from_env()
        if args.output_dir is not None:
            settings = replace(settings, output_dir=args.output_dir)
        providers = load_providers(args.providers)
        catalog_size = len(active_entries())
    except (ValueError, CatalogError, RegistryError, ProvidersError) as e:
        logger.error("The review cannot start: %s", e)
        return 1
    concepts = {name.strip() for name in args.concepts.split(",") if name.strip()} if args.concepts else None
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with WebClient() as web:
        result = run_review(settings, web, now, concepts, providers if args.terms else None)
    text = args.providers.read_text(encoding="utf-8")
    for check in result.terms:
        if check.status in ("changed", "recorded") and check.new_hash:
            text = set_terms_hash(text, check.provider, check.new_hash, now.date())
    if text != args.providers.read_text(encoding="utf-8"):
        args.providers.write_text(text, encoding="utf-8", newline="\n")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(build_report(result, now, catalog_size), encoding="utf-8", newline="\n")
    statuses = Counter(check.status for check in result.checks)
    logger.info("Source review: %s; %d candidates, %d unregistered regions; report written to %s.",
                ", ".join(f"{count} {status}" for status, count in sorted(statuses.items())) or "no entries",
                len(result.candidates), len(result.unregistered_regions), args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
