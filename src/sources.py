"""
The curated news outlets and press-release sources and their health status.

The sources are listed in ``config/sources.toml``, which the monthly source
review (``press_review.py``, run by ``source_review.py``) proposes changes to.
Items of a source with ``topic_filter`` (by default the general outlets) are
stored only if they come from a feed of a sociodemographic section or contain
a sociodemographic term; the items of specialist outlets and press sources are
stored without this filter, unless their entry sets it. Every source has the date
on which it was ``added``; a source that is no longer collected keeps its
entry with the date on which it was ``removed``, so that the coverage period
of every source stays known. ``[[excluded]]`` entries are candidates that
were checked and not admitted; the search does not propose them again, and
every review checks the entries with a temporary reason again.

The health of the sources is kept in ``state/sources.json`` and follows fixed rules:

- ``blocked``: the host refused automated requests (HTTP 401/403, challenge
  page, CAPTCHA). The source is not fetched again until the entry is reset by hand.
- ``tdm_reserved``: the host declares a text-and-data-mining reservation
  (TDMRep). The source is not fetched again until the entry is reset by hand.
- ``broken``: the feeds failed on BROKEN_AFTER_FAILURES consecutive runs. The
  source is retried once every BROKEN_RETRY_DAYS days and becomes active
  again when a feed works.
- ``active``: all other sources.
"""
import logging
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional, Union, get_args

from pydantic import BaseModel

from schema import ActorType, Jurisdiction, OutletType, PaywallModel, SocialTopic, StateCode
from state import MAX_ERROR_CHARS, atomic_write_text, dump_entries, load_entries

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "sources.toml"
SOURCES_STATE_FILE = "sources.json"
BROKEN_AFTER_FAILURES = 7
BROKEN_RETRY_DAYS = 7

SourceStatus = Literal["active", "broken", "blocked", "tdm_reserved"]
# Why a candidate was excluded. blocked, tdm_reserved, terms (the terms of the source forbid the reuse),
# duplicate and not_relevant are permanent until the entry is changed by hand; every source review checks
# the entries with the other reasons again.
ExclusionStatus = Literal["blocked", "tdm_reserved", "terms", "duplicate", "not_relevant", "no_feed", "inactive",
                          "robots"]
PERMANENT_EXCLUSIONS: tuple[str, ...] = ("blocked", "tdm_reserved", "terms", "duplicate", "not_relevant")
SourceKind = Literal["outlet", "press_source"]
# Outlet types whose whole output is about sociodemographic topics; their items need no section or term
SPECIALIST_OUTLET_TYPES = frozenset({"specialist_press"})
# Values of Jurisdiction: DE, the state codes, EU and INT
JURISDICTIONS: tuple[str, ...] = tuple(value for arg in get_args(Jurisdiction)
                                       for value in (get_args(arg) or (arg,)))


@dataclass(frozen=True)
class Outlet:
    """A news outlet.

    Attributes:
        id (str): Stable id.
        name (str): Display name.
        homepage (str): Homepage URL.
        outlet_type (str): Type of the outlet (see ``schema.OutletType``).
        owner_group (str): Publisher group or "public".
        home_state (Optional[str]): ISO 3166-2 state of a regional outlet.
        topic (Optional[str]): Topic of a specialist outlet (see ``schema.SocialTopic``).
        paywall_model (str): free, mixed or hard.
        feeds (tuple[str, ...]): RSS or Atom feed URLs.
        news_sitemap (Optional[str]): URL of the news sitemap.
        topic_filter (bool): Whether items need a sociodemographic section or term to be stored.
        fetch_pages (bool): Whether article pages may be fetched.
        added (Optional[date]): Day on which the outlet was added to the list.
        removed (Optional[date]): Day on which the outlet was removed; it is no longer collected.
        removed_reason (Optional[str]): Why the outlet was removed.
        keep (bool): True if the source review must not propose a removal for low yield.
    """
    id: str
    name: str
    homepage: str
    outlet_type: str
    owner_group: str
    paywall_model: str
    feeds: tuple[str, ...]
    home_state: Optional[str] = None
    topic: Optional[str] = None
    news_sitemap: Optional[str] = None
    topic_filter: bool = True
    fetch_pages: bool = False
    added: Optional[date] = None
    removed: Optional[date] = None
    removed_reason: Optional[str] = None
    keep: bool = False

    @property
    def active(self) -> bool:
        """True if the outlet is collected, i.e. it was not removed."""
        return self.removed is None


@dataclass(frozen=True)
class PressSource:
    """An issuer of press releases.

    Attributes:
        id (str): Stable id.
        name (str): Display name.
        homepage (str): Homepage URL.
        actor_type (str): Type of the actor (see ``schema.ActorType``).
        jurisdiction (str): DE, the ISO 3166-2 code of a state, EU or INT.
        feeds (tuple[str, ...]): RSS or Atom feed URLs.
        topic_filter (bool): Whether items need a sociodemographic section or term to be stored, for
            issuers whose releases cover other topics as well (e.g. the federal government).
        fetch_pages (bool): Whether press release pages may be fetched.
        added (Optional[date]): Day on which the source was added to the list.
        removed (Optional[date]): Day on which the source was removed; it is no longer collected.
        removed_reason (Optional[str]): Why the source was removed.
        keep (bool): True if the source review must not propose a removal for low yield.
    """
    id: str
    name: str
    homepage: str
    actor_type: str
    jurisdiction: str
    feeds: tuple[str, ...]
    topic_filter: bool = False
    fetch_pages: bool = False
    added: Optional[date] = None
    removed: Optional[date] = None
    removed_reason: Optional[str] = None
    keep: bool = False

    @property
    def active(self) -> bool:
        """True if the source is collected, i.e. it was not removed."""
        return self.removed is None


Source = Union[Outlet, PressSource]


@dataclass(frozen=True)
class ExcludedSource:
    """A candidate that was checked and not admitted.

    Attributes:
        id (str): Stable id.
        name (str): Display name.
        homepage (str): Homepage URL.
        status (str): Kind of the reason (see ``ExclusionStatus``).
        reason (str): Description of the reason.
        checked (date): Day on which the status was set.
        kind (Optional[str]): outlet or press_source; with the attributes below it lets the
            source review check the candidate again and admit it.
        feeds (tuple[str, ...]): Feed URLs that were proposed for the candidate.
        actor_type (Optional[str]): Actor type of a press source.
        jurisdiction (Optional[str]): Level of a press source (DE, state, EU, INT).
        outlet_type (Optional[str]): Type of an outlet.
        owner_group (Optional[str]): Publisher group of an outlet.
        home_state (Optional[str]): State of a regional outlet.
        topic (Optional[str]): Topic of a specialist outlet.
        paywall_model (Optional[str]): Paywall model of an outlet.
    """
    id: str
    name: str
    homepage: str
    status: str
    reason: str
    checked: date
    kind: Optional[str] = None
    feeds: tuple[str, ...] = ()
    actor_type: Optional[str] = None
    jurisdiction: Optional[str] = None
    outlet_type: Optional[str] = None
    owner_group: Optional[str] = None
    home_state: Optional[str] = None
    topic: Optional[str] = None
    paywall_model: Optional[str] = None

    @property
    def rechecked(self) -> bool:
        """True if every source review checks the candidate again: a temporary reason and a known kind."""
        return self.status not in PERMANENT_EXCLUSIONS and self.kind is not None


@dataclass(frozen=True)
class SourceConfig:
    """All curated sources.

    Attributes:
        outlets (tuple[Outlet, ...]): News outlets, including removed ones.
        press_sources (tuple[PressSource, ...]): Press-release sources, including removed ones.
        excluded (tuple[ExcludedSource, ...]): Candidates that were checked and not admitted.
    """
    outlets: tuple[Outlet, ...] = field(default_factory=tuple)
    press_sources: tuple[PressSource, ...] = field(default_factory=tuple)
    excluded: tuple[ExcludedSource, ...] = field(default_factory=tuple)

    @property
    def sources(self) -> tuple[Source, ...]:
        """All outlets and press sources in the order of the configuration, including removed ones."""
        return (*self.outlets, *self.press_sources)

    @property
    def active_sources(self) -> tuple[Source, ...]:
        """The outlets and press sources that are collected, in the order of the configuration."""
        return tuple(source for source in self.sources if source.active)


def _check_value(source_id: str, key: str, value: Optional[str], allowed: tuple[str, ...]) -> None:
    """Raises ValueError if a configured value is not in its vocabulary."""
    if value is not None and value not in allowed:
        raise ValueError(f"Source {source_id}: {key} {value!r} is not one of {', '.join(allowed)}")


def _date_value(source_id: str, item: dict[str, Any], key: str) -> Optional[date]:
    """Returns an optional TOML date of an entry.

    Raises:
        ValueError: If the value is not a date without time (e.g. ``added = 2026-10-19``).
    """
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"Source {source_id}: {key} must be a date such as 2026-10-19, got {value!r}")
    return value


def _lifecycle(source_id: str, item: dict[str, Any]) -> dict[str, Any]:
    """Reads the keys added, removed, removed_reason and keep of an entry."""
    return {
        "added": _date_value(source_id, item, "added"), "removed": _date_value(source_id, item, "removed"),
        "removed_reason": item.get("removed_reason"), "keep": bool(item.get("keep", False)),
    }


def load_config(path: Path = CONFIG_PATH) -> SourceConfig:
    """Reads and validates ``config/sources.toml``.

    Args:
        path: The configuration file.

    Returns:
        SourceConfig: The outlets, press sources and exclusions.

    Raises:
        ValueError: If an entry misses a key, uses an unknown value or a value that is
            not a date where a date is required, repeats an id, or a specialist outlet
            has no topic.
        FileNotFoundError: If the file does not exist.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    outlets = []
    for item in data.get("outlet", []):
        try:
            outlet = Outlet(
                id=item["id"], name=item["name"], homepage=item["homepage"], outlet_type=item["outlet_type"],
                owner_group=item["owner_group"], paywall_model=item["paywall_model"],
                feeds=tuple(item.get("feeds", [])), home_state=item.get("home_state"), topic=item.get("topic"),
                news_sitemap=item.get("news_sitemap"),
                topic_filter=bool(item.get("topic_filter", item["outlet_type"] not in SPECIALIST_OUTLET_TYPES)),
                fetch_pages=bool(item.get("fetch_pages", False)), **_lifecycle(str(item.get("id", "?")), item),
            )
        except KeyError as e:
            raise ValueError(f"Outlet {item.get('id', '?')} misses the key {e}") from e
        _check_value(outlet.id, "outlet_type", outlet.outlet_type, get_args(OutletType))
        _check_value(outlet.id, "paywall_model", outlet.paywall_model, get_args(PaywallModel))
        _check_value(outlet.id, "home_state", outlet.home_state, get_args(StateCode))
        _check_value(outlet.id, "topic", outlet.topic, get_args(SocialTopic))
        if (outlet.outlet_type in SPECIALIST_OUTLET_TYPES) != (outlet.topic is not None):
            raise ValueError(f"Outlet {outlet.id}: topic must be set exactly for specialist outlets")
        outlets.append(outlet)
    press_sources = []
    for item in data.get("press_source", []):
        try:
            source = PressSource(
                id=item["id"], name=item["name"], homepage=item["homepage"], actor_type=item["actor_type"],
                jurisdiction=item.get("jurisdiction", "DE"), feeds=tuple(item.get("feeds", [])),
                topic_filter=bool(item.get("topic_filter", False)), fetch_pages=bool(item.get("fetch_pages", False)),
                **_lifecycle(str(item.get("id", "?")), item),
            )
        except KeyError as e:
            raise ValueError(f"Press source {item.get('id', '?')} misses the key {e}") from e
        _check_value(source.id, "actor_type", source.actor_type, get_args(ActorType))
        _check_value(source.id, "jurisdiction", source.jurisdiction, JURISDICTIONS)
        press_sources.append(source)
    excluded = []
    for item in data.get("excluded", []):
        try:
            checked = _date_value(str(item.get("id", "?")), item, "checked")
            if checked is None:
                raise KeyError("checked")
            candidate = ExcludedSource(
                id=item["id"], name=item["name"], homepage=item["homepage"], status=item["status"],
                reason=item["reason"], checked=checked, kind=item.get("kind"), feeds=tuple(item.get("feeds", [])),
                actor_type=item.get("actor_type"), jurisdiction=item.get("jurisdiction"),
                outlet_type=item.get("outlet_type"), owner_group=item.get("owner_group"),
                home_state=item.get("home_state"), topic=item.get("topic"), paywall_model=item.get("paywall_model"),
            )
        except KeyError as e:
            raise ValueError(f"Excluded entry {item.get('id', '?')} misses the key {e}") from e
        _check_value(candidate.id, "status", candidate.status, get_args(ExclusionStatus))
        _check_value(candidate.id, "kind", candidate.kind, get_args(SourceKind))
        _check_value(candidate.id, "actor_type", candidate.actor_type, get_args(ActorType))
        _check_value(candidate.id, "jurisdiction", candidate.jurisdiction, JURISDICTIONS)
        _check_value(candidate.id, "outlet_type", candidate.outlet_type, get_args(OutletType))
        _check_value(candidate.id, "home_state", candidate.home_state, get_args(StateCode))
        _check_value(candidate.id, "topic", candidate.topic, get_args(SocialTopic))
        _check_value(candidate.id, "paywall_model", candidate.paywall_model, get_args(PaywallModel))
        excluded.append(candidate)
    ids = [entry.id for entry in (*outlets, *press_sources, *excluded)]
    duplicates = sorted({source_id for source_id in ids if ids.count(source_id) > 1})
    if duplicates:
        raise ValueError(f"Duplicate source ids in {path.name}: {', '.join(duplicates)}")
    return SourceConfig(outlets=tuple(outlets), press_sources=tuple(press_sources), excluded=tuple(excluded))


class SourceHealth(BaseModel):
    """Health of one source.

    Attributes:
        status (str): active, broken, blocked or tdm_reserved.
        consecutive_failures (int): Failed runs since the last success.
        last_attempt (Optional[date]): Day of the latest attempt.
        last_success (Optional[date]): Day of the latest successful attempt.
        last_error (Optional[str]): Error of the latest failed attempt.
        items_last_run (int): Items read in the latest successful attempt.
    """
    status: SourceStatus = "active"
    consecutive_failures: int = 0
    last_attempt: Optional[date] = None
    last_success: Optional[date] = None
    last_error: Optional[str] = None
    items_last_run: int = 0


class SourceHealthStore:
    """Health of all sources (``state/sources.json``)."""

    def __init__(self, path: Path, entries: dict[str, SourceHealth]) -> None:
        """Initializes the store.

        Args:
            path: The JSON file.
            entries: Health by source id.
        """
        self._path = path
        self._entries = entries

    @classmethod
    def load(cls, state_dir: Path) -> "SourceHealthStore":
        """Reads the health file; a missing file means all sources are active.

        Args:
            state_dir: Folder of the state files.

        Returns:
            SourceHealthStore: The loaded store.
        """
        path = state_dir / SOURCES_STATE_FILE
        entries = {key: SourceHealth.model_validate(value) for key, value in load_entries(path, "sources").items()}
        return cls(path, entries)

    def save(self) -> None:
        """Writes the file atomically."""
        atomic_write_text(
            self._path,
            dump_entries("sources", {key: value.model_dump(mode="json") for key, value in self._entries.items()}),
        )

    def get(self, source_id: str) -> SourceHealth:
        """Returns the health of a source; unknown sources are active."""
        return self._entries.get(source_id, SourceHealth())

    def is_due(self, source_id: str, today: date) -> bool:
        """Tells whether a source is fetched in this run.

        Args:
            source_id: The source.
            today: Date of the run.

        Returns:
            bool: True for active sources and for broken sources whose retry is due.
        """
        health = self.get(source_id)
        if health.status == "active":
            return True
        if health.status == "broken":
            return health.last_attempt is None or health.last_attempt <= today - timedelta(days=BROKEN_RETRY_DAYS)
        return False

    def record_success(self, source_id: str, today: date, items: int) -> None:
        """Records that at least one feed of a source worked.

        Args:
            source_id: The source.
            today: Date of the run.
            items: Number of items read.
        """
        self._entries[source_id] = self.get(source_id).model_copy(update={
            "status": "active", "consecutive_failures": 0, "last_attempt": today, "last_success": today,
            "last_error": None, "items_last_run": items,
        })

    def record_failure(self, source_id: str, today: date, error: str,
                       status: Optional[SourceStatus] = None) -> SourceHealth:
        """Records a failed attempt and applies the status rules.

        Args:
            source_id: The source.
            today: Date of the run.
            error: Description of the failure.
            status: blocked or tdm_reserved to set that status directly; None
                applies the broken rule.

        Returns:
            SourceHealth: The updated health.
        """
        health = self.get(source_id)
        failures = health.consecutive_failures + 1
        new_status: SourceStatus = status or ("broken" if failures >= BROKEN_AFTER_FAILURES else health.status)
        if new_status != health.status:
            logger.warning("Source %s is now %s: %s", source_id, new_status, error)
        updated = health.model_copy(update={
            "status": new_status, "consecutive_failures": failures, "last_attempt": today,
            "last_error": error[:MAX_ERROR_CHARS],
        })
        self._entries[source_id] = updated
        return updated

    def status_of(self, source: Source) -> str:
        """Returns the status of a configured source: removed, or its health status.

        Args:
            source: The source.

        Returns:
            str: removed, active, broken, blocked or tdm_reserved.
        """
        return "removed" if not source.active else self.get(source.id).status

    def status_counts(self, source_ids: list[str]) -> dict[str, int]:
        """Counts the sources per status.

        Args:
            source_ids: All configured source ids.

        Returns:
            dict[str, int]: Number of sources per status.
        """
        counts: dict[str, int] = {}
        for source_id in source_ids:
            status = self.get(source_id).status
            counts[status] = counts.get(status, 0) + 1
        return counts
