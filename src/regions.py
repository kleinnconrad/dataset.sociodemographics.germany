"""
The registry of the German regions (``config/regions.toml``).

Every ``[[region]]`` entry is one region of the NUTS classification of the
European Union: Germany (``DE``), the 16 states (NUTS 1, e.g. ``DE2``
Bayern), the 38 government regions (NUTS 2, e.g. ``DE21`` Oberbayern) and the
districts and district-free cities (NUTS 3, e.g. ``DE212`` München,
Kreisfreie Stadt). The level, the parent region and the ISO 3166-2 code of
the state follow from the code. ``nuts`` lists the versions of the
classification that contain the code with this territory: a district whose
territory changed gets a new code, and the old code keeps its entry, because
Eurostat datasets that still use the older version publish their data under
it.

The NUTS code is the join key ``geo`` of the dataset
``sociodemographic_series``; the state code links the regions to the state
keys of dataset.politics.germany and dataset.economics.germany. Entries are
never deleted: a region that is no longer collected keeps its entry with the
date on which it was ``removed`` and a ``removed_reason``.
"""
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "regions.toml"

COUNTRY = "DE"
GEO_PATTERN = re.compile(r"^DE(?:[1-9A-G](?:[0-9A-F](?:[0-9A-Z])?)?)?$")
NUTS_VERSIONS = ("2016", "2021", "2024")
LEVEL_BY_LENGTH = {2: "country", 3: "nuts1", 4: "nuts2", 5: "nuts3"}
# ISO 3166-2 code of every state by its NUTS 1 code
STATE_BY_NUTS1: dict[str, str] = {
    "DE1": "DE-BW", "DE2": "DE-BY", "DE3": "DE-BE", "DE4": "DE-BB", "DE5": "DE-HB", "DE6": "DE-HH", "DE7": "DE-HE",
    "DE8": "DE-MV", "DE9": "DE-NI", "DEA": "DE-NW", "DEB": "DE-RP", "DEC": "DE-SL", "DED": "DE-SN", "DEE": "DE-ST",
    "DEF": "DE-SH", "DEG": "DE-TH",
}
# Areas outside the registry that catalog entries can add for comparison
EXTRA_GEOS: dict[str, str] = {"EU27_2020": "eu"}
KNOWN_KEYS = frozenset({"geo", "name", "nuts", "added", "removed", "removed_reason", "notes"})


class RegistryError(ValueError):
    """The region registry contains invalid or duplicate entries."""


@dataclass(frozen=True)
class Region:
    """One region of the registry.

    Attributes:
        geo (str): NUTS code.
        name (str): Name in the NUTS classification.
        nuts_versions (tuple[str, ...]): NUTS versions that contain the code with this territory.
        added (date): Day on which the region was added.
        removed (Optional[date]): Day on which the region was removed; it is no longer collected.
        removed_reason (Optional[str]): Why the region was removed.
    """
    geo: str
    name: str
    nuts_versions: tuple[str, ...]
    added: date
    removed: Optional[date] = None
    removed_reason: Optional[str] = None

    @property
    def level(self) -> str:
        """country, nuts1, nuts2 or nuts3."""
        return LEVEL_BY_LENGTH[len(self.geo)]

    @property
    def parent(self) -> Optional[str]:
        """NUTS code of the region one level above; None for Germany."""
        return self.geo[:-1] if len(self.geo) > 2 else None

    @property
    def state(self) -> Optional[str]:
        """ISO 3166-2 code of the state that contains the region; None for Germany."""
        return state_of(self.geo)

    @property
    def active(self) -> bool:
        """True if the region is collected, i.e. it was not removed."""
        return self.removed is None


def state_of(geo: str) -> Optional[str]:
    """Returns the ISO 3166-2 code of the state that contains a German NUTS region.

    Args:
        geo: A NUTS code or another area code.

    Returns:
        Optional[str]: The state code, or None for Germany, aggregates and unknown codes.
    """
    return STATE_BY_NUTS1.get(geo[:3]) if geo.startswith(COUNTRY) and len(geo) > 2 else None


def level_of(geo: str) -> Optional[str]:
    """Returns the level of a geo code.

    Args:
        geo: A German NUTS code or an extra area code.

    Returns:
        Optional[str]: eu, country, nuts1, nuts2 or nuts3; None for an unknown code.
    """
    if geo in EXTRA_GEOS:
        return EXTRA_GEOS[geo]
    return LEVEL_BY_LENGTH.get(len(geo)) if GEO_PATTERN.match(geo) else None


def _optional_date(raw: dict[str, Any], key: str) -> Optional[date]:
    """Reads an optional TOML date."""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{key} must be a date (YYYY-MM-DD)")
    return value


def parse_region(raw: dict[str, Any]) -> Region:
    """Validates one registry entry.

    Args:
        raw: The entry as read from TOML.

    Returns:
        Region: The region.

    Raises:
        ValueError: If a field is missing, unknown or invalid.
    """
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        raise ValueError(f"unknown keys: {', '.join(unknown)}")
    geo = raw.get("geo")
    if not isinstance(geo, str) or not GEO_PATTERN.match(geo):
        raise ValueError(f"invalid NUTS code {geo!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    versions = raw.get("nuts")
    if (not isinstance(versions, list) or not versions
            or not all(isinstance(version, str) and version in NUTS_VERSIONS for version in versions)):
        raise ValueError(f"nuts must be a non-empty list of {', '.join(NUTS_VERSIONS)}")
    added = _optional_date(raw, "added")
    if added is None:
        raise ValueError("added is missing")
    removed = _optional_date(raw, "removed")
    removed_reason = raw.get("removed_reason")
    if (removed is None) != (removed_reason is None):
        raise ValueError("removed and removed_reason must be given together")
    return Region(geo=geo, name=name.strip(), nuts_versions=tuple(sorted(versions)), added=added, removed=removed,
                  removed_reason=removed_reason)


def load_regions(path: Path = CONFIG_PATH) -> list[Region]:
    """Reads and validates the registry.

    Args:
        path: The registry file.

    Returns:
        list[Region]: All regions in file order, including removed ones.

    Raises:
        RegistryError: If an entry is invalid, a code is duplicated or a parent region is missing.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("region", [])
    if not isinstance(entries, list):
        raise RegistryError(f"{path} must contain [[region]] entries")
    regions: list[Region] = []
    problems: list[str] = []
    for position, raw in enumerate(entries, start=1):
        try:
            regions.append(parse_region(raw))
        except ValueError as e:
            problems.append(f"entry {position} ({raw.get('geo', '?')}): {e}")
    duplicates = sorted(geo for geo, count in Counter(region.geo for region in regions).items() if count > 1)
    if duplicates:
        problems.append(f"duplicate codes: {', '.join(duplicates)}")
    codes = {region.geo for region in regions}
    orphans = sorted(region.geo for region in regions if region.parent and region.parent not in codes)
    if orphans:
        problems.append(f"regions without their parent region: {', '.join(orphans)}")
    if problems:
        raise RegistryError(f"{path}: " + "; ".join(problems))
    return regions


def active_regions(levels: Optional[set[str]] = None, path: Path = CONFIG_PATH) -> list[Region]:
    """Returns the regions that are collected, optionally of some levels.

    Args:
        levels: Levels to select (country, nuts1, nuts2, nuts3); None returns all levels.
        path: The registry file.

    Returns:
        list[Region]: The active regions in file order.
    """
    return [region for region in load_regions(path)
            if region.active and (levels is None or region.level in levels)]
