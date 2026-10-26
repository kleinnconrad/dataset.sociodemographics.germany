"""
Writes vintages of statistical series to the dataset ``sociodemographic_series``.

The series collectors (Eurostat, ECB) fetch the complete values of every
series of a catalog entry and pass them to ``SeriesWriter``. The writer
compares them with the values stored at the previous observation and emits a
record only if something changed:

- revision 0 holds all periods of the series (the first observation);
- a later revision holds the periods that were added or whose value changed,
  and lists the periods whose value disappeared in ``removed_periods``.

Replaying the revisions of a series in the order of ``observed_at`` gives the
series as it was known at any time t (a real-time data set), which avoids
look-ahead bias in models trained on revised data.

The current values of every series are kept in the seen store, one shard per
concept and state (``state/seen/sociodemographic_series/<concept>/<DE1>.json``;
national series and aggregates in a shard named after their code), so that a
run rewrites only the shards of the series that changed. A response that
would remove more than MAX_REMOVED_SHARE of the stored periods of a series is
treated as incomplete and not written.
"""
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from catalog import CatalogEntry
from collectors.base import RunContext
from regions import level_of, state_of
from schema import SociodemographicSeriesRecord
from state import SeenStore, content_hash

logger = logging.getLogger(__name__)

DATASET = "sociodemographic_series"
# A response that removes more than this share of the stored periods (and more than MIN_SUSPICIOUS_REMOVALS)
# is treated as incomplete
MAX_REMOVED_SHARE = 0.05
MIN_SUSPICIOUS_REMOVALS = 3
PERIOD_PATTERNS: dict[str, re.Pattern[str]] = {
    "W": re.compile(r"^\d{4}-W\d{2}$"),
    "M": re.compile(r"^\d{4}-\d{2}$"),
    "Q": re.compile(r"^\d{4}-Q[1-4]$"),
    "A": re.compile(r"^\d{4}$"),
}
# Period formats of the providers that differ from the stored format: 2026M09, 2026Q3, 2026W05
_COMPACT_PERIOD = re.compile(r"^(\d{4})([MQW])(\d{1,2})$")


class IncompleteSeriesError(Exception):
    """A response would remove too many stored periods; it is not written."""


@dataclass(frozen=True)
class SeriesKey:
    """One series of a catalog entry.

    Attributes:
        entry (CatalogEntry): The catalog entry.
        geo (str): Area code.
        codes (tuple[str, ...]): Breakdown codes in the order of ``entry.dimensions``.
    """
    entry: CatalogEntry
    geo: str
    codes: tuple[str, ...] = ()

    @property
    def series_id(self) -> str:
        """The stable id ``<concept>.<geo>[.<code>...]``."""
        return self.entry.series_id(self.geo, self.codes)

    @property
    def provider_key(self) -> str:
        """The SDMX key of the series at the provider."""
        return self.entry.provider_key(self.geo, self.codes)

    @property
    def shard(self) -> str:
        """Name of the seen shard: concept and NUTS 1 code, or concept and the area code of national series."""
        group = self.geo[:3] if self.geo.startswith("DE") and len(self.geo) > 2 else self.geo
        return f"{self.entry.concept}/{group}"


@dataclass
class SeriesData:
    """The values of a series as fetched from the provider.

    Attributes:
        values (dict[str, float]): Values by normalized period.
        url (str): URL of the series at the provider, stored as ``source_url``.
        source_updated (Optional[datetime]): Last update of the dataset at the provider (UTC).
    """
    values: dict[str, float]
    url: str
    source_updated: Optional[datetime] = None


def normalize_period(raw: str, frequency: str) -> Optional[str]:
    """Converts a period of a provider to the stored format of its frequency.

    Args:
        raw: The period, e.g. ``2026``, ``2026-09``, ``2026M09``, ``2026-Q3`` or ``2026-W05``.
        frequency: Frequency of the series (W, M, Q, A).

    Returns:
        Optional[str]: The period as YYYY-Www, YYYY-MM, YYYY-Qn or YYYY; None if the period does not match
        the frequency.
    """
    text = raw.strip()
    compact = _COMPACT_PERIOD.match(text)
    if compact:
        year, letter, number = compact.groups()
        text = f"{year}-Q{int(number)}" if letter == "Q" else f"{year}-{'W' if letter == 'W' else ''}{int(number):02d}"
    pattern = PERIOD_PATTERNS.get(frequency)
    return text if pattern is not None and pattern.match(text) else None


def parse_value(raw: Any) -> Optional[float]:
    """Parses an observation value.

    Args:
        raw: A number or a text such as ``2.25``; empty texts, ``NaN`` and markers of missing values give None.

    Returns:
        Optional[float]: The finite value, or None.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    return value if math.isfinite(value) else None


def period_delta(previous: Mapping[str, float], current: Mapping[str, float]) -> tuple[dict[str, float], list[str]]:
    """Compares the stored values of a series with the current ones.

    Args:
        previous: Stored values by period.
        current: Current values by period.

    Returns:
        tuple: The added or changed values by period, and the periods whose value disappeared.
    """
    changed = {period: value for period, value in current.items() if previous.get(period) != value}
    removed = sorted(period for period in previous if period not in current)
    return changed, removed


class SeriesWriter:
    """Compares fetched series with the stored ones and builds the vintage records."""

    def __init__(self, ctx: RunContext, source_id: str, license: str) -> None:
        """Initializes the writer.

        Args:
            ctx: The run context.
            source_id: Id of the source, stored in every record.
            license: License or terms of the source.
        """
        self._ctx = ctx
        self._source_id = source_id
        self._license = license

    def _seen(self, key: SeriesKey) -> SeenStore:
        """Returns the seen shard of a series."""
        return self._ctx.seen(DATASET, shard=key.shard)

    def stored_values(self, key: SeriesKey) -> dict[str, float]:
        """Returns the values of a series stored at the previous observation.

        Args:
            key: The series.

        Returns:
            dict[str, float]: Values by period; empty for a new series.
        """
        entry = self._seen(key).get(key.series_id)
        return dict(entry.data.get("values") or {}) if entry else {}

    def stored_series(self, entry: CatalogEntry, shards: set[str]) -> set[str]:
        """Returns the ids of the stored series of a catalog entry in some shards.

        Args:
            entry: The catalog entry.
            shards: Names of the shards to read.

        Returns:
            set[str]: Series ids with stored values.
        """
        ids: set[str] = set()
        for shard in shards:
            ids.update(series_id for series_id in self._ctx.seen(DATASET, shard=shard).keys()
                       if series_id.startswith(entry.concept + "."))
        return ids

    def build(self, key: SeriesKey, data: SeriesData) -> Optional[SociodemographicSeriesRecord]:
        """Builds the record of a new or changed series and stores its current values.

        Args:
            key: The series.
            data: The fetched values.

        Returns:
            Optional[SociodemographicSeriesRecord]: The record, or None if nothing changed or the series has no
            values yet.

        Raises:
            IncompleteSeriesError: If the response would remove too many stored periods.
        """
        seen = self._seen(key)
        values = dict(sorted(data.values.items()))
        item_hash = content_hash(values)
        status = seen.classify(key.series_id, item_hash)
        if status == "unchanged" or (status == "new" and not values):
            return None
        stored = self.stored_values(key)
        changed, removed = period_delta(stored, values)
        if len(removed) > max(MIN_SUSPICIOUS_REMOVALS, MAX_REMOVED_SHARE * len(stored)):
            raise IncompleteSeriesError(f"{key.series_id}: the response lacks {len(removed)} of {len(stored)} "
                                        f"stored periods")
        revision = seen.mark(key.series_id, item_hash, self._ctx.today, data={"values": values})
        periods = list(values)
        entry = key.entry
        return SociodemographicSeriesRecord(
            **self._ctx.envelope(DATASET, f"series:{key.series_id}", revision=revision, source_id=self._source_id,
                                 source_url=data.url, license=self._license, backfilled=revision == 0),
            series_id=key.series_id,
            concept=entry.concept,
            topic=entry.topic,
            geo=key.geo,
            geo_level=level_of(key.geo) or "country",
            state=state_of(key.geo),
            breakdown=dict(zip(entry.dimensions, key.codes)),
            frequency=entry.frequency,
            unit=entry.unit,
            provider=entry.provider,
            provider_flow=entry.flow,
            provider_key=key.provider_key,
            values=changed,
            removed_periods=removed,
            first_period=periods[0] if periods else None,
            last_period=periods[-1] if periods else None,
            observations=len(periods),
            source_updated=data.source_updated,
        )
