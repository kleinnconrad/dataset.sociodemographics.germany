"""
Writes the entries of the series catalog and the region registry to their datasets.

``config/series.toml`` and ``config/regions.toml`` are the slowly changing dimensions of the
dataset: every entry is written when it appears and again, with the next revision, when one of
its fields changes, so that the definition of a concept or region at time t can be
reconstructed from the data alone. The collectors send no requests.
"""
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from catalog import CONFIG_PATH as CATALOG_PATH
from catalog import CatalogEntry, load_catalog
from collectors.base import Collector, CollectorResult, RunContext
from regions import CONFIG_PATH as REGIONS_PATH
from regions import Region, load_regions
from schema import RecordBase, RegionRecord, SeriesCatalogRecord
from state import CollectorEntry, content_hash

logger = logging.getLogger(__name__)

SOURCE_ID = "registry"
LICENSE = "Configuration of this project; no license granted"
REPOSITORY_URL = "https://github.com/kleinnconrad/dataset.sociodemographics.germany/blob/main/"


def catalog_fields(entry: CatalogEntry) -> dict[str, Any]:
    """Returns the dataset fields of a catalog entry.

    Args:
        entry: The catalog entry.

    Returns:
        dict: The fields of a ``SeriesCatalogRecord`` without the common fields.
    """
    return {
        "concept": entry.concept, "name": entry.name, "topic": entry.topic, "frequency": entry.frequency,
        "unit": entry.unit, "provider": entry.provider, "provider_flow": entry.flow, "key_template": entry.key,
        "geo_levels": list(entry.geo_levels), "extra_geos": list(entry.extra_geos),
        "breakdown": {name: list(codes) for name, codes in entry.breakdown}, "added": entry.added,
        "removed": entry.removed, "removed_reason": entry.removed_reason,
    }


def region_fields(region: Region) -> dict[str, Any]:
    """Returns the dataset fields of a registry region.

    Args:
        region: The registry entry.

    Returns:
        dict: The fields of a ``RegionRecord`` without the common fields.
    """
    return {
        "geo": region.geo, "name": region.name, "level": region.level, "parent": region.parent,
        "state": region.state, "nuts_versions": list(region.nuts_versions), "added": region.added,
        "removed": region.removed, "removed_reason": region.removed_reason,
    }


def emit_changes(ctx: RunContext, dataset: str, items: Sequence[tuple[str, dict[str, Any]]],
                 model: Callable[..., RecordBase], source_url: str) -> dict[str, int]:
    """Emits a record for every item that is new or changed since the previous run.

    Args:
        ctx: The run context.
        dataset: Name of the dataset.
        items: Key and record fields of every item.
        model: Record model of the dataset.
        source_url: URL of the configuration file.

    Returns:
        dict[str, int]: Counts of all, new and changed items.
    """
    seen = ctx.seen(dataset)
    counters = {"items": len(items), "new": 0, "changed": 0}
    records = []
    for key, fields in items:
        item_hash = content_hash(fields)
        status = seen.classify(key, item_hash)
        if status == "unchanged":
            continue
        counters[status] += 1
        revision = seen.mark(key, item_hash, ctx.today)
        records.append(model(**ctx.envelope(dataset, f"{dataset}:{key}", revision=revision, source_id=SOURCE_ID,
                                            source_url=source_url, license=LICENSE, backfilled=False), **fields))
    ctx.emit(dataset, records)
    logger.info("%s: %d new, %d changed of %d entries.", dataset, counters["new"], counters["changed"], len(items))
    return counters


class SeriesCatalogCollector(Collector):
    """Writes new and changed entries of the series catalog."""
    name = "series_catalog"
    datasets = ("series_catalog",)

    def __init__(self, catalog_path: Path = CATALOG_PATH) -> None:
        """Initializes the collector.

        Args:
            catalog_path: The series catalog.
        """
        self._catalog_path = catalog_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Emits a record for every catalog entry that is new or changed.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters.
        """
        items = [(item.concept, catalog_fields(item)) for item in load_catalog(self._catalog_path)]
        return CollectorResult(counters=emit_changes(ctx, "series_catalog", items, SeriesCatalogRecord,
                                                     REPOSITORY_URL + "config/series.toml"))


class RegionsCollector(Collector):
    """Writes new and changed entries of the region registry."""
    name = "regions"
    datasets = ("regions",)

    def __init__(self, regions_path: Path = REGIONS_PATH) -> None:
        """Initializes the collector.

        Args:
            regions_path: The region registry.
        """
        self._regions_path = regions_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Emits a record for every region that is new or changed.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters.
        """
        items = [(region.geo, region_fields(region)) for region in load_regions(self._regions_path)]
        return CollectorResult(counters=emit_changes(ctx, "regions", items, RegionRecord,
                                                     REPOSITORY_URL + "config/regions.toml"))
