"""
Writes the entries of the series catalog, the region registry, the snapshot configuration and the press
configuration to their datasets.

``config/series.toml``, ``config/regions.toml``, ``config/snapshots.toml`` and ``config/sources.toml`` are the
slowly changing dimensions of the dataset: every entry is written when it appears and again, with
the next revision, when one of its fields changes, so that the definition of a concept,
region, snapshot item or news source at time t can be reconstructed from the data alone. The
collectors send no requests.
"""
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Optional

from catalog import CONFIG_PATH as CATALOG_PATH
from catalog import CatalogEntry, load_catalog
from collectors.base import Collector, CollectorResult, RunContext
from regions import CONFIG_PATH as REGIONS_PATH
from regions import Region, load_regions
from schema import PressCatalogRecord, RecordBase, RegionRecord, SeriesCatalogRecord, SnapshotCatalogRecord
from snapshots import CONFIG_PATH as SNAPSHOTS_PATH
from snapshots import SnapshotSource, load_snapshot_config
from sources import CONFIG_PATH as SOURCES_PATH
from sources import Outlet, Source, load_config
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


def snapshot_fields(source: SnapshotSource) -> dict[str, Any]:
    """Returns the dataset fields of a snapshot entry.

    Args:
        source: The snapshot entry.

    Returns:
        dict: The fields of a ``SnapshotCatalogRecord`` without the common fields.
    """
    return {
        "snapshot_id": source.id, "name": source.name, "topic": source.topic, "provider": source.provider,
        "format": source.format, "address": source.address, "geo_scheme": source.geo_scheme,
        "archive": source.archive, "added": source.added, "removed": source.removed,
        "removed_reason": source.removed_reason,
    }


def press_fields(source: Source) -> dict[str, Any]:
    """Returns the dataset fields of an outlet or press source.

    Args:
        source: The entry of the press configuration.

    Returns:
        dict: The fields of a ``PressCatalogRecord`` without the common fields.

    Raises:
        ValueError: If the entry has no ``added`` date.
    """
    if source.added is None:
        raise ValueError(f"Source {source.id} has no added date")
    outlet = source if isinstance(source, Outlet) else None
    return {
        "press_id": source.id, "kind": "outlet" if outlet else "press_source", "name": source.name,
        "homepage": source.homepage, "outlet_type": outlet.outlet_type if outlet else None,
        "owner_group": outlet.owner_group if outlet else None, "home_state": outlet.home_state if outlet else None,
        "outlet_topic": outlet.topic if outlet else None, "paywall_model": outlet.paywall_model if outlet else None,
        "actor_type": None if outlet else source.actor_type, "jurisdiction": None if outlet else source.jurisdiction,
        "feeds": list(source.feeds), "news_sitemap": outlet.news_sitemap if outlet else None,
        "topic_filter": source.topic_filter, "fetch_pages": source.fetch_pages, "added": source.added,
        "removed": source.removed, "removed_reason": source.removed_reason,
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


class SnapshotCatalogCollector(Collector):
    """Writes new and changed entries of the snapshot configuration."""
    name = "snapshot_catalog"
    datasets = ("snapshot_catalog",)

    def __init__(self, config_path: Path = SNAPSHOTS_PATH, providers: Optional[set[str]] = None) -> None:
        """Initializes the collector.

        Args:
            config_path: The snapshot configuration.
            providers: Provider ids; None reads them from ``config/providers.toml``.
        """
        self._config_path = config_path
        self._providers = providers

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Emits a record for every snapshot entry that is new or changed.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters.
        """
        sources, _excluded = load_snapshot_config(self._config_path, self._providers)
        items = [(source.id, snapshot_fields(source)) for source in sources]
        return CollectorResult(counters=emit_changes(ctx, "snapshot_catalog", items, SnapshotCatalogRecord,
                                                     REPOSITORY_URL + "config/snapshots.toml"))


class PressCatalogCollector(Collector):
    """Writes new and changed entries of the press configuration (news outlets and press sources)."""
    name = "press_catalog"
    datasets = ("press_catalog",)

    def __init__(self, config_path: Path = SOURCES_PATH) -> None:
        """Initializes the collector.

        Args:
            config_path: The press configuration.
        """
        self._config_path = config_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Emits a record for every outlet and press source that is new or changed.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters.
        """
        items = [(source.id, press_fields(source)) for source in load_config(self._config_path).sources]
        return CollectorResult(counters=emit_changes(ctx, "press_catalog", items, PressCatalogRecord,
                                                     REPOSITORY_URL + "config/sources.toml"))
