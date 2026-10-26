"""
The configuration of the snapshot sources (``config/snapshots.toml``).

A snapshot source publishes data without a reliable history: it overwrites a
table or file with every update, removes older editions, publishes only the
current state or changes figures without versioning. The pipeline stores what
such a source published at every observation in the dataset
``source_snapshots`` and thereby builds the history that the source does not
keep.

Every ``[[snapshot]]`` entry describes one published item:
- a table of a GENESIS database (``format = "genesis"``, ``table``): GENESIS-Online
  of the Statistisches Bundesamt (provider ``destatis``), the Regionaldatenbank
  Deutschland (``regionalstatistik``) or the Zensus 2022 database (``zensus2022``);
- a file or page (``csv``, ``xlsx``, ``json``, ``html``, ``pdf``) at a fixed
  address (``url``) or linked from a page (``page`` and the regular expression
  ``link`` of the file address), which is how files with changing names are
  found.

``[[excluded]]`` entries record sources that were checked and not admitted,
with the day of the check and the reason, so that they are not proposed again
without a new check.
"""
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional, get_args

from providers import load_providers
from schema import GeoScheme, SeriesTopic, SnapshotFormat

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "snapshots.toml"

ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[A-Za-z0-9_.-]+$")
KNOWN_KEYS = frozenset({
    "id", "name", "topic", "provider", "format", "table", "url", "page", "link", "expect", "geo_scheme", "archive",
    "check_days", "update_days",
    "startyear", "regionalvariable", "regionalkey", "csv", "xlsx", "html", "added", "removed", "removed_reason", "notes",
})
EXCLUDED_KEYS = frozenset({"id", "name", "url", "checked", "reason", "notes"})
GENESIS_PROVIDERS = frozenset({"destatis", "regionalstatistik", "zensus2022"})


class SnapshotConfigError(ValueError):
    """The snapshot configuration contains invalid or duplicate entries."""


@dataclass(frozen=True)
class SnapshotSource:
    """One published item that the pipeline observes.

    Attributes:
        id (str): Stable id ``<provider>.<item>``.
        name (str): Name of the item.
        topic (str): Topic group (see ``schema.SeriesTopic``).
        provider (str): Provider id of ``config/providers.toml``.
        format (str): genesis, csv, xlsx, json, html or pdf.
        geo_scheme (str): Region codes in the content (see ``schema.GeoScheme``).
        added (date): Day on which the entry was added; start of collection.
        table (Optional[str]): Table code of a GENESIS database.
        url (Optional[str]): Fixed address of a file or page.
        page (Optional[str]): Page that links the current file.
        link (Optional[str]): Regular expression of the file address on ``page``.
        expect (Optional[str]): Regular expression that the title of a GENESIS table (or, for a file, its name
            and column names) must match; a response without a match is not stored, so that a changed or wrong
            code or link cannot write other data under the id.
        archive (bool): Whether the published file itself is stored in ``data/files/``.
        check_days (int): Days between two checks; 0 checks in every run.
        update_days (Optional[int]): Days within which the source is expected to change the content; the
            monthly review reports an item whose content did not change for longer.
        options (dict): Format options: ``startyear``, ``regionalvariable``, ``regionalkey`` for GENESIS
            tables; ``csv`` (delimiter, encoding, skip_lines, header_pattern), ``xlsx`` (sheet, sheet_pattern,
            header_row or header_pattern, header_rows) and ``html`` (table) for files.
        removed (Optional[date]): Day on which the entry was removed.
        removed_reason (Optional[str]): Why the entry was removed.
    """
    id: str
    name: str
    topic: str
    provider: str
    format: str
    geo_scheme: str
    added: date
    table: Optional[str] = None
    url: Optional[str] = None
    page: Optional[str] = None
    link: Optional[str] = None
    expect: Optional[str] = None
    archive: bool = False
    check_days: int = 0
    update_days: Optional[int] = None
    options: dict[str, Any] = field(default_factory=dict)
    removed: Optional[date] = None
    removed_reason: Optional[str] = None

    @property
    def active(self) -> bool:
        """True if the entry is collected, i.e. it was not removed."""
        return self.removed is None

    def item(self, url: Optional[str] = None) -> str:
        """Id of the item at the provider: the table code, or the file name of the fetched address.

        Args:
            url: The address from which the content was fetched; None uses the configured address.

        Returns:
            str: The table code or the file name without query.
        """
        if self.table:
            return self.table
        address = (url or self.url or self.page or "").split("?", 1)[0].split("#", 1)[0]
        return address.rstrip("/").rsplit("/", 1)[-1] or address

    @property
    def address(self) -> str:
        """Where the item is read, as text for the catalog."""
        if self.table:
            return f"{self.provider} table {self.table}"
        if self.url:
            return self.url
        return f"{self.page} (link matching {self.link})"


@dataclass(frozen=True)
class ExcludedSource:
    """A source that was checked and not admitted.

    Attributes:
        id (str): Id of the source.
        name (str): Name of the source.
        url (str): Address that was checked.
        checked (date): Day of the check.
        reason (str): Why the source is not collected.
    """
    id: str
    name: str
    url: str
    checked: date
    reason: str


def _date(raw: dict[str, Any], key: str, required: bool = False) -> Optional[date]:
    """Reads a TOML date."""
    value = raw.get(key)
    if value is None:
        if required:
            raise ValueError(f"{key} is missing")
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{key} must be a date (YYYY-MM-DD)")
    return value


def _text(raw: dict[str, Any], key: str, required: bool = True) -> Optional[str]:
    """Reads a string."""
    value = raw.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def parse_snapshot(raw: dict[str, Any], providers: set[str]) -> SnapshotSource:
    """Validates one ``[[snapshot]]`` entry.

    Args:
        raw: The entry as read from TOML.
        providers: Provider ids of ``config/providers.toml``.

    Returns:
        SnapshotSource: The source.

    Raises:
        ValueError: If a field is missing, unknown or invalid.
    """
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        raise ValueError(f"unknown keys: {', '.join(unknown)}")
    source_id = _text(raw, "id")
    assert source_id is not None
    if not ID_PATTERN.match(source_id):
        raise ValueError("id must have the form <provider>.<item>")
    provider = _text(raw, "provider")
    if provider not in providers:
        raise ValueError(f"provider {provider!r} has no entry in config/providers.toml")
    if not source_id.startswith(f"{provider}."):
        raise ValueError(f"id must start with the provider {provider!r}")
    topic = _text(raw, "topic")
    if topic not in get_args(SeriesTopic):
        raise ValueError(f"unknown topic {topic!r}")
    file_format = _text(raw, "format")
    if file_format not in get_args(SnapshotFormat):
        raise ValueError(f"unknown format {file_format!r}")
    geo_scheme = _text(raw, "geo_scheme")
    if geo_scheme not in get_args(GeoScheme):
        raise ValueError(f"unknown geo_scheme {geo_scheme!r}")
    table, url, page, link, expect = (_text(raw, key, required=False)
                                      for key in ("table", "url", "page", "link", "expect"))
    if expect:
        re.compile(expect)
    if file_format == "genesis":
        if provider not in GENESIS_PROVIDERS or not table or url or page:
            raise ValueError(f"a genesis entry needs a table and one of the providers {', '.join(sorted(GENESIS_PROVIDERS))}")
        if not expect:
            raise ValueError("a genesis entry needs expect, a pattern of the table title")
    else:
        if table or bool(url) == bool(page) or bool(page) != bool(link):
            raise ValueError("a file entry needs either url, or page and link")
        if link:
            re.compile(link)
    check_days = raw.get("check_days", 0)
    if not isinstance(check_days, int) or check_days < 0:
        raise ValueError("check_days must be a non-negative integer")
    update_days = raw.get("update_days")
    if update_days is not None and (not isinstance(update_days, int) or update_days < 1):
        raise ValueError("update_days must be a positive integer")
    archive = raw.get("archive", file_format == "pdf")
    if not isinstance(archive, bool):
        raise ValueError("archive must be true or false")
    if file_format == "pdf" and not archive:
        raise ValueError("a pdf entry must be archived")
    options = {key: raw[key] for key in ("startyear", "regionalvariable", "regionalkey", "csv", "xlsx", "html")
               if key in raw}
    added = _date(raw, "added", required=True)
    removed = _date(raw, "removed")
    removed_reason = raw.get("removed_reason")
    if (removed is None) != (removed_reason is None):
        raise ValueError("removed and removed_reason must be given together")
    assert added is not None
    return SnapshotSource(
        id=source_id, name=_text(raw, "name") or "", topic=topic, provider=provider, format=file_format,
        geo_scheme=geo_scheme, added=added, table=table, url=url, page=page, link=link, expect=expect, archive=archive,
        check_days=check_days, update_days=update_days, options=options, removed=removed,
        removed_reason=removed_reason,
    )


def parse_excluded(raw: dict[str, Any]) -> ExcludedSource:
    """Validates one ``[[excluded]]`` entry.

    Args:
        raw: The entry as read from TOML.

    Returns:
        ExcludedSource: The entry.

    Raises:
        ValueError: If a field is missing or unknown.
    """
    unknown = sorted(set(raw) - EXCLUDED_KEYS)
    if unknown:
        raise ValueError(f"unknown keys: {', '.join(unknown)}")
    checked = _date(raw, "checked", required=True)
    assert checked is not None
    return ExcludedSource(id=_text(raw, "id") or "", name=_text(raw, "name") or "", url=_text(raw, "url") or "",
                          checked=checked, reason=_text(raw, "reason") or "")


def load_snapshot_config(path: Path = CONFIG_PATH,
                         providers: Optional[set[str]] = None) -> tuple[list[SnapshotSource], list[ExcludedSource]]:
    """Reads and validates the snapshot configuration.

    Args:
        path: The configuration file.
        providers: Provider ids; None reads them from ``config/providers.toml``.

    Returns:
        tuple: The snapshot sources in file order (including removed ones) and the excluded sources.

    Raises:
        SnapshotConfigError: If an entry is invalid or an id is duplicated.
    """
    if providers is None:
        providers = {provider.id for provider in load_providers()}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    sources: list[SnapshotSource] = []
    excluded: list[ExcludedSource] = []
    problems: list[str] = []
    for position, raw in enumerate(data.get("snapshot", []), start=1):
        try:
            sources.append(parse_snapshot(raw, providers))
        except ValueError as e:
            problems.append(f"snapshot {position} ({raw.get('id', '?')}): {e}")
    for position, raw in enumerate(data.get("excluded", []), start=1):
        try:
            excluded.append(parse_excluded(raw))
        except ValueError as e:
            problems.append(f"excluded {position} ({raw.get('id', '?')}): {e}")
    duplicates = sorted(key for key, count in Counter([*(s.id for s in sources), *(e.id for e in excluded)]).items()
                        if count > 1)
    if duplicates:
        problems.append(f"duplicate ids: {', '.join(duplicates)}")
    if problems:
        raise SnapshotConfigError(f"{path}: " + "; ".join(problems))
    return sources, excluded


def active_snapshots(formats: Optional[set[str]] = None, path: Path = CONFIG_PATH,
                     providers: Optional[set[str]] = None) -> list[SnapshotSource]:
    """Returns the snapshot sources that are collected, optionally of some formats.

    Args:
        formats: Formats to select; None returns all.
        path: The configuration file.
        providers: Provider ids; None reads them from ``config/providers.toml``.

    Returns:
        list[SnapshotSource]: The active sources in file order.
    """
    sources, _excluded = load_snapshot_config(path, providers)
    return [source for source in sources if source.active and (formats is None or source.format in formats)]
