"""
The providers of the statistical series and their terms of use (``config/providers.toml``).

Every ``[[provider]]`` entry names an organization and interface, the terms of
reuse that the records carry in their field ``license``, the required
attribution, and the page with the terms. The monthly source review fetches
every terms page and compares the hash of its text with ``terms_hash``; a
change is proposed in the review pull request, so that the terms are read
again before the change is merged.
"""
import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional, get_args

from schema import Provider as ProviderId

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "providers.toml"
KNOWN_KEYS = frozenset({"id", "name", "api", "license", "attribution", "terms_url", "terms_hash", "terms_hash_since",
                        "notes"})


class ProvidersError(ValueError):
    """The provider configuration is invalid."""


@dataclass(frozen=True)
class Provider:
    """A provider of statistical series.

    Attributes:
        id (str): Provider id of the catalog and the records.
        name (str): Name of the organization.
        api (str): Base URL of the interface.
        license (str): Terms of reuse as stored in the records.
        attribution (str): Required attribution.
        terms_url (str): Page with the terms of reuse.
        terms_hash (Optional[str]): Hash of the text of the terms page as last reviewed.
        terms_hash_since (Optional[date]): Day on which the hash was recorded.
    """
    id: str
    name: str
    api: str
    license: str
    attribution: str
    terms_url: str
    terms_hash: Optional[str] = None
    terms_hash_since: Optional[date] = None


def _required_str(raw: dict[str, Any], key: str) -> str:
    """Reads a required non-empty string."""
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def load_providers(path: Path = CONFIG_PATH) -> list[Provider]:
    """Reads and validates the provider configuration.

    Args:
        path: The configuration file.

    Returns:
        list[Provider]: The providers in file order.

    Raises:
        ProvidersError: If an entry is invalid or an id is unknown or duplicated.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    providers: list[Provider] = []
    problems: list[str] = []
    for position, raw in enumerate(data.get("provider", []), start=1):
        try:
            unknown = sorted(set(raw) - KNOWN_KEYS)
            if unknown:
                raise ValueError(f"unknown keys: {', '.join(unknown)}")
            provider_id = _required_str(raw, "id")
            if provider_id not in get_args(ProviderId):
                raise ValueError(f"unknown provider id {provider_id!r}")
            since = raw.get("terms_hash_since")
            if since is not None and (not isinstance(since, date) or isinstance(since, datetime)):
                raise ValueError("terms_hash_since must be a date")
            terms_hash = raw.get("terms_hash")
            if terms_hash is not None and not isinstance(terms_hash, str):
                raise ValueError("terms_hash must be a string")
            providers.append(Provider(
                id=provider_id, name=_required_str(raw, "name"), api=_required_str(raw, "api"),
                license=_required_str(raw, "license"), attribution=_required_str(raw, "attribution"),
                terms_url=_required_str(raw, "terms_url"), terms_hash=terms_hash or None, terms_hash_since=since,
            ))
        except ValueError as e:
            problems.append(f"entry {position} ({raw.get('id', '?')}): {e}")
    ids = [provider.id for provider in providers]
    duplicates = sorted({provider_id for provider_id in ids if ids.count(provider_id) > 1})
    if duplicates:
        problems.append(f"duplicate ids: {', '.join(duplicates)}")
    missing = sorted(set(get_args(ProviderId)) - set(ids))
    if missing:
        problems.append(f"providers without an entry: {', '.join(missing)}")
    if problems:
        raise ProvidersError(f"{path}: " + "; ".join(problems))
    return providers


def set_terms_hash(text: str, provider_id: str, terms_hash: str, day: date) -> str:
    """Sets ``terms_hash`` and ``terms_hash_since`` of one provider in the text of the configuration.

    The other lines, including comments, stay unchanged.

    Args:
        text: The configuration file.
        provider_id: Id of the provider.
        terms_hash: The new hash.
        day: Day of the review.

    Returns:
        str: The changed text.

    Raises:
        ValueError: If the provider has no entry.
    """
    lines = text.split("\n")
    starts = [index for index, line in enumerate(lines) if line.strip() == "[[provider]]"]
    for number, start in enumerate(starts):
        end = starts[number + 1] if number + 1 < len(starts) else len(lines)
        block = lines[start:end]
        if not any(re.fullmatch(rf'id\s*=\s*"{re.escape(provider_id)}"', line.strip()) for line in block):
            continue
        kept = [line for line in block if not re.match(r"\s*terms_hash(_since)?\s*=", line)]
        position = next((index + 1 for index, line in enumerate(kept) if line.strip().startswith("terms_url")),
                        len(kept))
        kept[position:position] = [f'terms_hash = "{terms_hash}"', f"terms_hash_since = {day.isoformat()}"]
        return "\n".join(lines[:start] + kept + lines[end:])
    raise ValueError(f"provider {provider_id!r} has no entry")
