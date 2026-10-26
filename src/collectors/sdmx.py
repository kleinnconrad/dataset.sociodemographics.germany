"""
Collects statistical series from the SDMX 2.1 REST interfaces of Eurostat and the ECB.

Sources:
- Eurostat dissemination API (``ec.europa.eu/eurostat/api/dissemination/sdmx/2.1``):
  population by age, sex and region, demographic balance, fertility, life expectancy, deaths,
  migration, asylum and citizenship, education, income distribution, poverty and living
  conditions, housing, regional labour market and regional accounts. Eurostat authorises the
  reuse of its data provided the source is acknowledged.
- ECB Data Portal (``data-api.ecb.europa.eu``): the Distributional Wealth Accounts (shares of
  net wealth of the wealthiest and the poorest households). ECB statistics may be reused free
  of charge provided the source is acknowledged.

Both interfaces are documented APIs without a key; they are called with
``respect_robots=False`` and at most one request per second per host.

Fetch strategy: one request per catalog entry and group of at most ``max_geos`` areas. The
key of a request lists the areas and the breakdown codes of the entry as SDMX OR-lists, e.g.
``A.NR.M+F.Y_LT5+Y5-9.DE1+DE2``, and the response format SDMX-CSV has one row per
observation. The dimension columns of a row are matched with the positions of the key
template, so that the parser does not depend on the column names of a provider. A group
without data returns HTTP 404 and is counted; an entry without data in any group is
``not_found``.

Eurostat states the last update of every dataset in its table of contents. A catalog entry
whose dataset has not changed since the entry was last fetched is skipped, and fetched again
at the latest after ``FULL_CHECK_DAYS``. Without the table of contents every entry is fetched.

Records: vintages of each series in ``sociodemographic_series`` (see ``collectors/series.py``).

History: all periods that the provider publishes, on the first run.

Schedule: every run, because statistics are released on many days of the month and
``observed_at`` should be close to the release. At the run deadline the collector stops; the
remaining entries follow in the next run.
"""
import csv
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Optional

from catalog import CONFIG_PATH as CATALOG_PATH
from catalog import CatalogEntry, active_entries
from collectors.base import Collector, CollectorResult, RunContext, batches
from collectors.series import (
    DATASET,
    IncompleteSeriesError,
    SeriesData,
    SeriesKey,
    SeriesWriter,
    normalize_period,
    parse_value,
)
from regions import CONFIG_PATH as REGIONS_PATH
from regions import Region, active_regions
from state import CollectorEntry, content_hash
from web import BlockedError, FetchError, NotFoundError, RateLimitError, WebClient

logger = logging.getLogger(__name__)

# Eurostat states the last update of a dataset in this column, e.g. 06/02/26 23:00:00
LAST_UPDATE_COLUMN = "LAST UPDATE"
# Columns before TIME_PERIOD that are not dimensions of the key (SDMX-CSV 1.0 and 2.0, ECB csvdata)
NON_DIMENSION_COLUMNS = frozenset({"DATAFLOW", LAST_UPDATE_COLUMN, "KEY", "STRUCTURE", "STRUCTURE_ID", "ACTION"})
REQUEST_TIMEOUT_S = 120.0
EUROSTAT_TOC_URL = "https://ec.europa.eu/eurostat/api/dissemination/catalogue/toc/txt"
MAX_FAILURES_LISTED = 20


@dataclass(frozen=True)
class SdmxProvider:
    """An SDMX 2.1 REST interface.

    Attributes:
        name (str): Provider name in the catalog and collector name.
        source_id (str): Source id stored in the records.
        license (str): License or terms stored in the records.
        url_template (str): URL of a data query with the placeholders ``{flow}`` and ``{key}``.
        params (tuple): Query parameters that select SDMX-CSV.
        headers (tuple): Request headers that select SDMX-CSV.
        max_geos (int): Maximum number of areas in the key of one request, so that URLs stay short.
    """
    name: str
    source_id: str
    license: str
    url_template: str
    params: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    max_geos: int = 50

    def url(self, flow: str, key: str) -> str:
        """Returns the URL of a data query without the format parameters."""
        return self.url_template.format(flow=flow, key=key)


EUROSTAT = SdmxProvider(
    name="eurostat", source_id="eurostat", license="Eurostat (reuse authorised with acknowledgement of the source)",
    url_template="https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{flow}/{key}",
    params=(("format", "SDMX-CSV"),),
)
ECB = SdmxProvider(
    name="ecb", source_id="ecb_data_portal", license="ECB (reuse free of charge with acknowledgement of the source)",
    url_template="https://data-api.ecb.europa.eu/service/data/{flow}/{key}",
    params=(("format", "csvdata"),), max_geos=20,
)
PROVIDERS: dict[str, SdmxProvider] = {provider.name: provider for provider in (EUROSTAT, ECB)}


@dataclass(frozen=True)
class TocEntry:
    """A dataset in the table of contents of Eurostat.

    Attributes:
        code (str): Dataset code, e.g. ``demo_r_pjangrp3``.
        title (str): Title of the dataset.
        last_update (Optional[date]): Last update of the data.
        structure_change (Optional[date]): Last change of the table structure.
        data_start (str): First period of the data.
        data_end (str): Last period of the data.
    """
    code: str
    title: str
    last_update: Optional[date]
    structure_change: Optional[date]
    data_start: str
    data_end: str


@dataclass
class EntryData:
    """The series of a catalog entry as fetched from the provider.

    Attributes:
        series (dict[SeriesKey, SeriesData]): Values per series that has at least one value.
        requests (int): Requests sent.
        empty_groups (int): Groups of areas for which the provider has no data.
        unexpected_rows (int): Rows whose codes the entry did not request; they are ignored.
    """
    series: dict[SeriesKey, SeriesData] = field(default_factory=dict)
    requests: int = 0
    empty_groups: int = 0
    unexpected_rows: int = 0


def parse_timestamp(value: str) -> Optional[datetime]:
    """Parses the last-update column of Eurostat (``dd/mm/yy HH:MM:SS``) or an ISO timestamp.

    Args:
        value: The text.

    Returns:
        Optional[datetime]: The time in UTC; times without zone are taken as UTC. None if invalid.
    """
    text = value.strip()
    for pattern in ("%d/%m/%y %H:%M:%S", None):
        try:
            parsed = datetime.strptime(text, pattern) if pattern else datetime.fromisoformat(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def parse_toc_date(value: str) -> Optional[date]:
    """Parses a date of the Eurostat table of contents (``dd.mm.yyyy`` or ISO).

    Args:
        value: The text.

    Returns:
        Optional[date]: The date, or None if the text is empty or invalid.
    """
    text = value.strip()
    for parse in (lambda: datetime.strptime(text, "%d.%m.%Y").date(), lambda: date.fromisoformat(text[:10])):
        try:
            return parse()
        except ValueError:
            continue
    return None


def parse_toc(text: str) -> dict[str, TocEntry]:
    """Reads the table of contents of Eurostat in its tab-separated text form.

    Args:
        text: The file with a header row (title, code, type, last update of data, last table structure
            change, data start, data end, ...).

    Returns:
        dict[str, TocEntry]: Datasets and tables by code; folders are left out.

    Raises:
        ValueError: If the header lacks the code or the last-update column.
    """
    reader = csv.reader(io.StringIO(text.lstrip("﻿")), delimiter="\t", quotechar='"')
    header = [name.strip().lower() for name in next(reader, [])]
    try:
        positions = {name: header.index(name) for name in ("title", "code", "type", "last update of data")}
    except ValueError as e:
        raise ValueError(f"unexpected header of the table of contents: {header[:8]}") from e
    optional = {name: header.index(name) if name in header else None
                for name in ("last table structure change", "data start", "data end")}

    def cell(row: list[str], position: Optional[int]) -> str:
        return row[position].strip() if position is not None and position < len(row) else ""

    entries: dict[str, TocEntry] = {}
    for row in reader:
        if len(row) <= positions["last update of data"] or cell(row, positions["type"]) == "folder":
            continue
        code = cell(row, positions["code"])
        if not code:
            continue
        entries[code] = TocEntry(
            code=code, title=" ".join(cell(row, positions["title"]).split()),
            last_update=parse_toc_date(cell(row, positions["last update of data"])),
            structure_change=parse_toc_date(cell(row, optional["last table structure change"])),
            data_start=cell(row, optional["data start"]), data_end=cell(row, optional["data end"]),
        )
    return entries


def fetch_eurostat_toc(web: WebClient) -> dict[str, TocEntry]:
    """Fetches the table of contents of Eurostat.

    Args:
        web: The HTTP client.

    Returns:
        dict[str, TocEntry]: Datasets by code.

    Raises:
        FetchError: If the request fails.
        ValueError: If the response is not the expected table.
    """
    response = web.get(EUROSTAT_TOC_URL, params={"lang": "en"}, respect_robots=False, timeout_s=REQUEST_TIMEOUT_S)
    return parse_toc(response.text)


def parse_sdmx_csv(text: str, entry: CatalogEntry, geos: Sequence[str]) -> tuple[dict[tuple[str, tuple[str, ...]], dict[str, float]], int, Optional[str]]:
    """Reads the observations of the series of a catalog entry from an SDMX-CSV response.

    The delimiter is a comma or a semicolon; a byte order mark is ignored. The dimension
    columns are the columns before ``TIME_PERIOD`` except the non-dimension columns; their
    number must equal the number of parts of the key template.

    Args:
        text: The CSV text.
        entry: The catalog entry.
        geos: The requested areas; rows of other areas are ignored.

    Returns:
        tuple: Values by period per (geo, breakdown codes), the number of ignored rows with codes that were
        not requested, and the last update of the dataset if the response states it.

    Raises:
        ValueError: If the CSV lacks TIME_PERIOD or OBS_VALUE, has another number of dimensions than the key,
            or a fixed dimension has another code than the key.
    """
    text = text.lstrip("﻿")
    first_line = text.split("\n", 1)[0]
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    columns = reader.fieldnames or []
    if "TIME_PERIOD" not in columns or "OBS_VALUE" not in columns:
        raise ValueError(f"not an SDMX-CSV response (columns: {', '.join(columns[:8])})")
    dimensions = [name for name in columns[:columns.index("TIME_PERIOD")] if name not in NON_DIMENSION_COLUMNS]
    parts = entry.key_parts
    if len(dimensions) != len(parts):
        raise ValueError(f"the response has {len(dimensions)} dimensions ({', '.join(dimensions)}); "
                         f"the key template {entry.key} has {len(parts)}")
    positions = entry.placeholder_positions()
    geo_column = dimensions[positions["geo"]]
    code_columns = [dimensions[positions[name]] for name in entry.dimensions]
    allowed = [set(codes) for _name, codes in entry.breakdown]
    fixed = [(dimensions[position], part) for position, part in enumerate(parts) if position not in positions.values()]
    wanted_geos = set(geos)
    series: dict[tuple[str, tuple[str, ...]], dict[str, float]] = {}
    unexpected = 0
    last_update: Optional[str] = None
    for row in reader:
        for column, code in fixed:
            if (row.get(column) or "") != code:
                raise ValueError(f"column {column} has the code {row.get(column)!r}; the key requests {code!r}")
        geo = row.get(geo_column) or ""
        codes = tuple(row.get(column) or "" for column in code_columns)
        if geo not in wanted_geos or any(code not in options for code, options in zip(codes, allowed)):
            unexpected += 1
            continue
        if last_update is None and (row.get(LAST_UPDATE_COLUMN) or "").strip():
            last_update = str(row[LAST_UPDATE_COLUMN])
        period = normalize_period(row.get("TIME_PERIOD") or "", entry.frequency)
        value = parse_value(row.get("OBS_VALUE"))
        if period is not None and value is not None:
            series.setdefault((geo, codes), {})[period] = value
    return series, unexpected, last_update


def fetch_entry(web: WebClient, provider: SdmxProvider, entry: CatalogEntry, geos: Sequence[str],
                start_period: Optional[str] = None) -> EntryData:
    """Fetches all series of a catalog entry, in groups of at most ``provider.max_geos`` areas.

    Args:
        web: The HTTP client.
        provider: The SDMX interface.
        entry: The catalog entry.
        geos: The areas to fetch.
        start_period: First period to fetch (SDMX ``startPeriod``, e.g. ``2023``); None fetches all periods.

    Returns:
        EntryData: The series with values.

    Raises:
        NotFoundError: If the provider has data for none of the groups.
        FetchError: If a request fails.
        ValueError: If a response is not a valid SDMX-CSV of the entry.
    """
    data = EntryData()
    last_not_found: Optional[NotFoundError] = None
    params = dict(provider.params)
    if start_period:
        params["startPeriod"] = start_period
    for group in batches(list(geos), provider.max_geos):
        url = provider.url(entry.flow, entry.request_key(group))
        data.requests += 1
        try:
            response = web.get(url, params=params or None, headers=dict(provider.headers) or None,
                               respect_robots=False, timeout_s=REQUEST_TIMEOUT_S)
        except NotFoundError as e:
            data.empty_groups += 1
            last_not_found = e
            continue
        values, unexpected, last_update = parse_sdmx_csv(response.text, entry, group)
        data.unexpected_rows += unexpected
        updated = parse_timestamp(last_update) if last_update else None
        for (geo, codes), series_values in values.items():
            key = SeriesKey(entry, geo, codes)
            data.series[key] = SeriesData(values=series_values, url=provider.url(entry.flow, key.provider_key),
                                          source_updated=updated)
    if last_not_found is not None and data.empty_groups == data.requests:
        raise last_not_found
    return data


def entry_fingerprint(entry: CatalogEntry, geos: Sequence[str]) -> str:
    """Returns a hash of everything that determines the requests of a catalog entry.

    Args:
        entry: The catalog entry.
        geos: The areas it is collected for.

    Returns:
        str: The hash; a changed entry or region list gives another hash.
    """
    return content_hash({"flow": entry.flow, "key": entry.key, "breakdown": entry.breakdown, "geos": list(geos),
                         "frequency": entry.frequency})


class SdmxCollector(Collector):
    """Writes vintages of the catalog series of one SDMX provider (set by the subclasses)."""
    datasets = (DATASET,)
    provider: ClassVar[SdmxProvider]
    # Whether the collector reads the Eurostat table of contents to skip unchanged datasets
    uses_toc: ClassVar[bool] = False

    def __init__(self, catalog_path: Path = CATALOG_PATH, regions_path: Path = REGIONS_PATH) -> None:
        """Initializes the collector.

        Args:
            catalog_path: The series catalog.
            regions_path: The region registry.
        """
        self._catalog_path = catalog_path
        self._regions_path = regions_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches every catalog entry of the provider and emits the new and changed vintages.

        Args:
            ctx: The run context.
            entry: Run history of this collector; its cursor holds per concept the dataset update and
                the day of the last complete fetch.

        Returns:
            CollectorResult: Cursor and counters; an error if all entries failed or the provider refused
            the requests.
        """
        catalog = active_entries(self.provider.name, self._catalog_path)
        regions = active_regions(path=self._regions_path)
        toc: Optional[dict[str, TocEntry]] = None
        if self.uses_toc and not ctx.backfill:
            try:
                toc = fetch_eurostat_toc(ctx.web)
            except (FetchError, ValueError) as e:
                logger.warning("Eurostat table of contents not available; all entries are fetched: %s", e)
        return collect_entries(ctx, self.provider, catalog, regions, entry.cursor, toc)


class EurostatCollector(SdmxCollector):
    """Writes vintages of the Eurostat series of the catalog."""
    name = "eurostat"
    provider = EUROSTAT
    uses_toc = True


class EcbCollector(SdmxCollector):
    """Writes vintages of the ECB series of the catalog."""
    name = "ecb"
    provider = ECB


def _is_unchanged(ctx: RunContext, previous: dict[str, Any], toc: Optional[dict[str, TocEntry]],
                  entry: CatalogEntry, fingerprint: str) -> bool:
    """Tells whether an entry can be skipped because its dataset did not change since its last complete fetch."""
    if toc is None or entry.flow not in toc or toc[entry.flow].last_update is None:
        return False
    if previous.get("fingerprint") != fingerprint or not previous.get("checked"):
        return False
    if previous.get("dataset_updated") != toc[entry.flow].last_update.isoformat():
        return False
    return (ctx.today - date.fromisoformat(previous["checked"])).days < ctx.settings.full_check_days


def collect_entries(ctx: RunContext, provider: SdmxProvider, catalog: Sequence[CatalogEntry],
                    regions: Sequence[Region], cursor: dict[str, Any],
                    toc: Optional[dict[str, TocEntry]] = None) -> CollectorResult:
    """Fetches the catalog entries one after another and emits the vintages of their series.

    A quota stops the collector until the next run, a refusal stops it with an error, and a single
    failing entry is counted and skipped.

    Args:
        ctx: The run context.
        provider: The SDMX interface.
        catalog: The entries to fetch.
        regions: The active regions of the registry.
        cursor: Cursor of the previous run (``{"entries": {concept: {...}}}``).
        toc: The Eurostat table of contents, used to skip unchanged datasets; None fetches every entry.

    Returns:
        CollectorResult: New cursor, counters, completeness and an error message if needed.
    """
    writer = SeriesWriter(ctx, provider.source_id, provider.license)
    states: dict[str, Any] = dict(cursor.get("entries") or {})
    counters: dict[str, Any] = {"entries": len(catalog), "unchanged_datasets": 0, "requests": 0, "series": 0,
                                "new": 0, "revised": 0, "unchanged": 0, "absent": 0, "incomplete": 0, "values": 0,
                                "not_found": 0, "failed": 0}
    complete = True
    error: Optional[str] = None
    not_found: list[str] = []
    failures: list[str] = []
    for position, entry in enumerate(catalog):
        if ctx.past_deadline():
            counters["deferred"] = len(catalog) - position
            complete = False
            break
        geos = entry.geos(regions)
        fingerprint = entry_fingerprint(entry, geos)
        previous = states.get(entry.concept, {})
        if _is_unchanged(ctx, previous, toc, entry, fingerprint):
            counters["unchanged_datasets"] += 1
            continue
        try:
            data = fetch_entry(ctx.web, provider, entry, geos)
        except RateLimitError as e:
            logger.warning("%s rate limit at %s; the remaining entries follow in the next run: %s", provider.name,
                           entry.concept, e)
            counters["deferred"] = len(catalog) - position
            complete = False
            break
        except BlockedError as e:
            logger.error("%s refused the request; no further requests in this run: %s", provider.name, e)
            error = f"blocked: {e}"
            complete = False
            break
        except NotFoundError as e:
            logger.warning("%s entry %s has no data (discontinued dataset or wrong key): %s", provider.name,
                           entry.concept, e)
            counters["not_found"] += 1
            not_found.append(entry.concept)
            continue
        except (FetchError, ValueError, KeyError) as e:
            logger.warning("%s entry %s failed: %s", provider.name, entry.concept, e)
            counters["failed"] += 1
            failures.append(f"{entry.concept}: {e}"[:300])
            continue
        counters["requests"] += data.requests
        counters["series"] += len(data.series)
        records = []
        for key, series_data in data.series.items():
            try:
                record = writer.build(key, series_data)
            except IncompleteSeriesError as e:
                logger.warning("%s series not written: %s", provider.name, e)
                counters["incomplete"] += 1
                continue
            if record is None:
                counters["unchanged"] += 1
                continue
            counters["new" if record.revision == 0 else "revised"] += 1
            counters["values"] += len(record.values)
            records.append(record)
        fetched_ids = {key.series_id for key in data.series}
        shards = {SeriesKey(entry, geo).shard for geo in geos}
        counters["absent"] += len(writer.stored_series(entry, shards) - fetched_ids)
        ctx.emit(DATASET, records)
        ctx.save_seen()
        if data.unexpected_rows:
            logger.info("%s entry %s: %d rows with codes that were not requested were ignored.", provider.name,
                        entry.concept, data.unexpected_rows)
        states[entry.concept] = {
            "fingerprint": fingerprint, "checked": ctx.today.isoformat(), "series": len(data.series),
            "dataset_updated": (toc[entry.flow].last_update.isoformat()
                                if toc and entry.flow in toc and toc[entry.flow].last_update else None),
        }
    if not_found:
        counters["not_found_entries"] = not_found
    if failures:
        counters["failed_entries"] = failures[:MAX_FAILURES_LISTED]
    attempted = len(catalog) - counters.get("deferred", 0) - counters["unchanged_datasets"]
    unavailable = counters["failed"] + counters["not_found"]
    if error is None and attempted and unavailable == attempted:
        error = f"all {attempted} entries failed or had no data"
    logger.info("%s: %d series in %d requests; %d new and %d revised series (%d values), %d unchanged, "
                "%d entries skipped as unchanged datasets, %d without data, %d failed.", provider.name,
                counters["series"], counters["requests"], counters["new"], counters["revised"], counters["values"],
                counters["unchanged"], counters["unchanged_datasets"], counters["not_found"], counters["failed"])
    known = {entry.concept for entry in catalog}
    return CollectorResult(cursor={"entries": {concept: state for concept, state in states.items() if concept in known}},
                           counters=counters, complete=complete, error=error)
