"""
The catalog of statistical series (``config/series.toml``).

Every ``[[series]]`` entry describes one concept, e.g. the population by
five-year age group and sex, at one provider: the dataset or dataflow
(``flow``), the SDMX key template (``key``), the levels of the German regions
(``geo_levels``) and further areas (``geos``) it is collected for, and the
codes of its breakdown dimensions (``breakdown``). The key template has one
placeholder for the region (``{geo}``) and one per breakdown dimension, e.g.
``A.NR.{sex}.{age}.{geo}``; the other positions are fixed codes. Every
combination of a region and the breakdown codes is a series with the stable id
``<concept>.<geo>[.<code>...]``, e.g. ``population_by_age_group.DE212.F.Y20-24``,
the join key ``series_id`` of the dataset ``sociodemographic_series``.

Every entry has the date on which it was ``added``. An entry that is no
longer collected keeps its place with the date on which it was ``removed``
and a ``removed_reason``, so that the coverage period of every series stays
known; concepts and series ids are never reused for a different meaning.
"""
import itertools
import re
import tomllib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from math import prod
from pathlib import Path
from typing import Any, Optional, get_args

from regions import EXTRA_GEOS, Region
from schema import Frequency, GeoLevel, Provider, SeriesTopic

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "series.toml"

CONCEPT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# Codes of the providers: letters, digits and the characters _ - (no dots, which separate the parts of ids and keys)
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
PLACEHOLDER_PATTERN = re.compile(r"^\{([a-z][a-z0-9_]*)\}$")
GEO_DIMENSION = "geo"
REGION_LEVELS = frozenset(level for level in get_args(GeoLevel) if level != "eu")
KNOWN_KEYS = frozenset({
    "concept", "name", "topic", "frequency", "unit", "provider", "flow", "key", "geo_levels", "geos", "breakdown",
    "added", "removed", "removed_reason", "notes",
})


class CatalogError(ValueError):
    """The catalog file contains invalid or duplicate entries."""


@dataclass(frozen=True)
class CatalogEntry:
    """One concept of the catalog and the series it expands to.

    Attributes:
        concept (str): Stable id of the entry, the first part of its series ids.
        name (str): Name of the concept.
        topic (str): Topic group (see ``schema.SeriesTopic``).
        frequency (str): W, M, Q or A.
        unit (str): Unit of the values.
        provider (str): Provider (see ``schema.Provider``).
        flow (str): Dataset or dataflow of the provider.
        key (str): SDMX key template with ``{geo}`` and one placeholder per breakdown dimension.
        geo_levels (tuple[str, ...]): Levels of the German regions to collect.
        extra_geos (tuple[str, ...]): Further areas to collect, e.g. EU27_2020.
        breakdown (tuple[tuple[str, tuple[str, ...]], ...]): Codes per breakdown dimension, in the
            order of the placeholders in the key.
        added (date): Day on which the entry was added; start of collection.
        removed (Optional[date]): Day on which the entry was removed; it is no longer collected.
        removed_reason (Optional[str]): Why the entry was removed.
    """
    concept: str
    name: str
    topic: str
    frequency: str
    unit: str
    provider: str
    flow: str
    key: str
    geo_levels: tuple[str, ...]
    extra_geos: tuple[str, ...]
    breakdown: tuple[tuple[str, tuple[str, ...]], ...]
    added: date
    removed: Optional[date] = None
    removed_reason: Optional[str] = None

    @property
    def active(self) -> bool:
        """True if the entry is collected, i.e. it was not removed."""
        return self.removed is None

    @property
    def dimensions(self) -> tuple[str, ...]:
        """Names of the breakdown dimensions in the order of the key."""
        return tuple(name for name, _codes in self.breakdown)

    @property
    def key_parts(self) -> list[str]:
        """The parts of the key template, separated at the dots."""
        return self.key.split(".")

    def geos(self, regions: Sequence[Region]) -> list[str]:
        """Returns the areas the entry is collected for.

        Args:
            regions: The active regions of the registry.

        Returns:
            list[str]: The codes of the regions of ``geo_levels`` in registry order, then the extra areas.
        """
        return [region.geo for region in regions if region.level in self.geo_levels] + list(self.extra_geos)

    def combinations(self) -> list[tuple[str, ...]]:
        """Returns every combination of breakdown codes, in catalog order.

        Returns:
            list[tuple[str, ...]]: One tuple per series of a region; a single empty tuple without breakdown.
        """
        return list(itertools.product(*(codes for _name, codes in self.breakdown)))

    def expected_series(self, regions: Sequence[Region]) -> int:
        """Returns the number of series the entry can have.

        Args:
            regions: The active regions of the registry.

        Returns:
            int: Areas times combinations of breakdown codes.
        """
        return len(self.geos(regions)) * prod(len(codes) for _name, codes in self.breakdown)

    def series_id(self, geo: str, codes: Sequence[str]) -> str:
        """Returns the stable id of a series.

        Args:
            geo: Area code.
            codes: Breakdown codes in the order of ``dimensions``.

        Returns:
            str: ``<concept>.<geo>[.<code>...]``.
        """
        return ".".join([self.concept, geo, *codes])

    def fill_key(self, geo: str, codes: dict[str, str]) -> str:
        """Fills the key template with one value or an SDMX OR-list (``A+B``) per placeholder.

        Args:
            geo: Value of ``{geo}``.
            codes: Value per breakdown dimension.

        Returns:
            str: The SDMX key.
        """
        values = {GEO_DIMENSION: geo, **codes}
        parts = []
        for part in self.key_parts:
            match = PLACEHOLDER_PATTERN.match(part)
            parts.append(values[match.group(1)] if match else part)
        return ".".join(parts)

    def provider_key(self, geo: str, codes: Sequence[str]) -> str:
        """Returns the SDMX key of one series.

        Args:
            geo: Area code.
            codes: Breakdown codes in the order of ``dimensions``.

        Returns:
            str: The key, e.g. ``A.NR.F.Y20-24.DE212``.
        """
        return self.fill_key(geo, dict(zip(self.dimensions, codes)))

    def request_key(self, geos: Sequence[str]) -> str:
        """Returns the SDMX key that selects all series of some areas in one request.

        Args:
            geos: Area codes.

        Returns:
            str: The key with OR-lists, e.g. ``A.NR.M+F.Y_LT5+Y5-9.DE1+DE2``.
        """
        return self.fill_key("+".join(geos), {name: "+".join(codes) for name, codes in self.breakdown})

    def placeholder_positions(self) -> dict[str, int]:
        """Returns the position of every placeholder in the key.

        Returns:
            dict[str, int]: Position (from 0) by placeholder name, including ``geo``.
        """
        positions = {}
        for position, part in enumerate(self.key_parts):
            match = PLACEHOLDER_PATTERN.match(part)
            if match:
                positions[match.group(1)] = position
        return positions


def _required_str(raw: dict[str, Any], key: str) -> str:
    """Reads a required non-empty string."""
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_date(raw: dict[str, Any], key: str) -> Optional[date]:
    """Reads an optional TOML date."""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{key} must be a date (YYYY-MM-DD)")
    return value


def _str_list(raw: dict[str, Any], key: str) -> list[str]:
    """Reads an optional list of strings."""
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{key} must be a list of strings")
    duplicates = sorted(item for item, count in Counter(value).items() if count > 1)
    if duplicates:
        raise ValueError(f"{key} has duplicates: {', '.join(duplicates)}")
    return value


def _parse_key(key: str, breakdown: dict[str, list[str]]) -> list[str]:
    """Checks the placeholders and fixed parts of a key template.

    Args:
        key: The key template.
        breakdown: Codes per breakdown dimension.

    Returns:
        list[str]: The breakdown dimensions in the order of their placeholders.

    Raises:
        ValueError: If a placeholder is missing, unknown or repeated, or a fixed part is empty or a list.
    """
    placeholders = []
    for part in key.split("."):
        match = PLACEHOLDER_PATTERN.match(part)
        if match:
            placeholders.append(match.group(1))
        elif not part or "+" in part or "{" in part or "}" in part:
            raise ValueError(f"key part {part!r} must be one fixed code or a placeholder")
    repeated = sorted(name for name, count in Counter(placeholders).items() if count > 1)
    if repeated:
        raise ValueError(f"repeated placeholders: {', '.join(repeated)}")
    if GEO_DIMENSION not in placeholders:
        raise ValueError("key needs the placeholder {geo}")
    missing = sorted(set(breakdown) - set(placeholders))
    unknown = sorted(set(placeholders) - set(breakdown) - {GEO_DIMENSION})
    if missing:
        raise ValueError(f"breakdown dimensions without placeholder: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"placeholders without breakdown codes: {', '.join(unknown)}")
    return [name for name in placeholders if name != GEO_DIMENSION]


def parse_entry(raw: dict[str, Any]) -> CatalogEntry:
    """Validates one catalog entry.

    Args:
        raw: The entry as read from TOML.

    Returns:
        CatalogEntry: The entry.

    Raises:
        ValueError: If a field is missing, unknown or invalid.
    """
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        raise ValueError(f"unknown keys: {', '.join(unknown)}")
    concept = _required_str(raw, "concept")
    if not CONCEPT_PATTERN.match(concept):
        raise ValueError("concept must be lower-case snake_case")
    topic = _required_str(raw, "topic")
    if topic not in get_args(SeriesTopic):
        raise ValueError(f"unknown topic {topic!r}")
    frequency = _required_str(raw, "frequency")
    if frequency not in get_args(Frequency):
        raise ValueError(f"unknown frequency {frequency!r}")
    provider = _required_str(raw, "provider")
    if provider not in get_args(Provider):
        raise ValueError(f"unknown provider {provider!r}")
    added = _optional_date(raw, "added")
    if added is None:
        raise ValueError("added is missing")
    removed = _optional_date(raw, "removed")
    removed_reason = raw.get("removed_reason")
    if (removed is None) != (removed_reason is None):
        raise ValueError("removed and removed_reason must be given together")
    if removed is not None and removed < added:
        raise ValueError("removed is earlier than added")
    geo_levels = _str_list(raw, "geo_levels")
    bad_levels = sorted(set(geo_levels) - REGION_LEVELS)
    if bad_levels:
        raise ValueError(f"unknown geo levels: {', '.join(bad_levels)}")
    extra_geos = _str_list(raw, "geos")
    bad_geos = sorted(set(extra_geos) - set(EXTRA_GEOS))
    if bad_geos:
        raise ValueError(f"unknown areas in geos: {', '.join(bad_geos)} (known: {', '.join(EXTRA_GEOS)})")
    if not geo_levels and not extra_geos:
        raise ValueError("give geo_levels, geos or both")
    raw_breakdown = raw.get("breakdown", {})
    if not isinstance(raw_breakdown, dict):
        raise ValueError("breakdown must be a table of code lists")
    breakdown: dict[str, list[str]] = {}
    for name in raw_breakdown:
        values = _str_list(raw_breakdown, name)
        if not values:
            raise ValueError(f"breakdown.{name} is empty")
        bad_codes = [code for code in values if not CODE_PATTERN.match(code)]
        if bad_codes:
            raise ValueError(f"invalid codes in breakdown.{name}: {', '.join(bad_codes)}")
        breakdown[name] = values
    key = _required_str(raw, "key")
    order = _parse_key(key, breakdown)
    return CatalogEntry(
        concept=concept, name=_required_str(raw, "name"), topic=topic, frequency=frequency,
        unit=_required_str(raw, "unit"), provider=provider, flow=_required_str(raw, "flow"), key=key,
        geo_levels=tuple(geo_levels), extra_geos=tuple(extra_geos),
        breakdown=tuple((name, tuple(breakdown[name])) for name in order), added=added, removed=removed,
        removed_reason=removed_reason,
    )


def load_catalog(path: Path = CONFIG_PATH) -> list[CatalogEntry]:
    """Reads and validates the catalog.

    Args:
        path: The catalog file.

    Returns:
        list[CatalogEntry]: All entries in file order, including removed ones.

    Raises:
        CatalogError: If an entry is invalid or a concept is duplicated.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_entries = data.get("series", [])
    if not isinstance(raw_entries, list):
        raise CatalogError(f"{path} must contain [[series]] entries")
    entries: list[CatalogEntry] = []
    problems: list[str] = []
    for position, raw in enumerate(raw_entries, start=1):
        try:
            entries.append(parse_entry(raw))
        except ValueError as e:
            problems.append(f"entry {position} ({raw.get('concept', '?')}): {e}")
    duplicates = sorted(concept for concept, count in Counter(entry.concept for entry in entries).items()
                        if count > 1)
    if duplicates:
        problems.append(f"duplicate concepts: {', '.join(duplicates)}")
    if problems:
        raise CatalogError(f"{path}: " + "; ".join(problems))
    return entries


def active_entries(provider: Optional[str] = None, path: Path = CONFIG_PATH) -> list[CatalogEntry]:
    """Returns the entries that are collected, optionally of one provider.

    Args:
        provider: Provider to select; None returns the entries of all providers.
        path: The catalog file.

    Returns:
        list[CatalogEntry]: The active entries in file order.
    """
    return [entry for entry in load_catalog(path) if entry.active and (provider is None or entry.provider == provider)]
