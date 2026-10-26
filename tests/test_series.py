"""Tests of the vintage writer and the period and value helpers."""
import pytest

from catalog import load_catalog
from collectors.series import (
    IncompleteSeriesError,
    SeriesData,
    SeriesKey,
    SeriesWriter,
    normalize_period,
    parse_value,
    period_delta,
)
from conftest import CATALOG_FIXTURE


@pytest.mark.parametrize("raw, frequency, expected", [
    ("2025", "A", "2025"),
    ("2026-09", "M", "2026-09"),
    ("2026M09", "M", "2026-09"),
    ("2026M9", "M", "2026-09"),
    ("2026-Q3", "Q", "2026-Q3"),
    ("2026Q3", "Q", "2026-Q3"),
    ("2026-W05", "W", "2026-W05"),
    ("2026W5", "W", "2026-W05"),
    ("2026-09", "A", None),
    ("2026", "M", None),
])
def test_normalize_period(raw: str, frequency: str, expected) -> None:
    assert normalize_period(raw, frequency) == expected


@pytest.mark.parametrize("raw, expected", [("2.5", 2.5), (3, 3.0), ("", None), ("NaN", None), (":", None),
                                           (None, None), (True, None)])
def test_parse_value(raw, expected) -> None:
    assert parse_value(raw) == expected


def test_period_delta_lists_changed_and_removed_periods() -> None:
    changed, removed = period_delta({"2024": 1.0, "2025": 2.0, "2023": 0.5}, {"2024": 1.0, "2025": 2.5, "2026": 3.0})
    assert changed == {"2025": 2.5, "2026": 3.0}
    assert removed == ["2023"]


def test_series_keys_use_one_shard_per_concept_and_state() -> None:
    entry = next(item for item in load_catalog(CATALOG_FIXTURE) if item.concept == "population")
    assert SeriesKey(entry, "DE212", ("F",)).shard == "population/DE2"
    assert SeriesKey(entry, "DE2", ("F",)).shard == "population/DE2"
    assert SeriesKey(entry, "DE", ("F",)).shard == "population/DE"
    assert SeriesKey(entry, "EU27_2020", ("F",)).shard == "population/EU27_2020"


def test_writer_detects_unchanged_and_incomplete_series(make_ctx) -> None:
    entry = next(item for item in load_catalog(CATALOG_FIXTURE) if item.concept == "total_fertility_rate")
    writer = SeriesWriter(make_ctx(), "eurostat", "test")
    key = SeriesKey(entry, "DE")
    values = {str(year): 1.4 for year in range(2000, 2025)}
    first = writer.build(key, SeriesData(values=values, url="u"))
    assert first is not None and first.revision == 0 and first.observations == 25
    assert writer.build(key, SeriesData(values=dict(values), url="u")) is None
    assert writer.build(SeriesKey(entry, "DE1"), SeriesData(values={}, url="u")) is None
    with pytest.raises(IncompleteSeriesError):
        writer.build(key, SeriesData(values={"2024": 1.4}, url="u"))
