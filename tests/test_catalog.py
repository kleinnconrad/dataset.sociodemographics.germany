"""Tests of the series catalog: validation, expansion to series and keys."""
from datetime import date

import pytest

from catalog import CatalogError, active_entries, load_catalog, parse_entry
from conftest import CATALOG_FIXTURE, REGIONS_FIXTURE
from regions import active_regions

BASE = {
    "concept": "population", "name": "Population", "topic": "population_structure", "frequency": "A",
    "unit": "persons", "provider": "eurostat", "flow": "demo_r_pjanaggr3", "key": "A.NR.{sex}.{age}.{geo}",
    "geo_levels": ["country", "nuts1"], "breakdown": {"sex": ["M", "F"], "age": ["TOTAL", "Y_LT15"]},
    "added": date(2026, 10, 26),
}


def test_fixture_catalog_loads_with_removed_entries() -> None:
    entries = load_catalog(CATALOG_FIXTURE)
    assert [entry.concept for entry in entries] == ["population", "total_fertility_rate", "asylum_applicants",
                                                    "net_wealth_share", "old_concept"]
    assert [entry.concept for entry in active_entries("ecb", CATALOG_FIXTURE)] == ["net_wealth_share"]
    assert not load_catalog(CATALOG_FIXTURE)[-1].active


def test_entry_expands_to_series_ids_and_keys() -> None:
    entry = parse_entry(BASE)
    regions = active_regions(path=REGIONS_FIXTURE)
    assert entry.dimensions == ("sex", "age")
    assert entry.geos(regions) == ["DE", "DE1", "DE2"]
    assert entry.expected_series(regions) == 12
    assert entry.combinations()[:2] == [("M", "TOTAL"), ("M", "Y_LT15")]
    assert entry.series_id("DE1", ("F", "Y_LT15")) == "population.DE1.F.Y_LT15"
    assert entry.provider_key("DE1", ("F", "Y_LT15")) == "A.NR.F.Y_LT15.DE1"
    assert entry.request_key(["DE", "DE1"]) == "A.NR.M+F.TOTAL+Y_LT15.DE+DE1"
    assert entry.placeholder_positions() == {"sex": 2, "age": 3, "geo": 4}


def test_breakdown_order_follows_the_key() -> None:
    entry = parse_entry({**BASE, "key": "A.NR.{age}.{sex}.{geo}"})
    assert entry.dimensions == ("age", "sex")
    assert entry.series_id("DE", ("TOTAL", "M")) == "population.DE.TOTAL.M"


def test_entry_without_breakdown_has_one_series_per_area() -> None:
    entry = parse_entry({**BASE, "key": "A.NR.T.TOTAL.{geo}", "breakdown": {}, "geos": ["EU27_2020"]})
    regions = active_regions(path=REGIONS_FIXTURE)
    assert entry.combinations() == [()]
    assert entry.geos(regions) == ["DE", "DE1", "DE2", "EU27_2020"]
    assert entry.series_id("EU27_2020", ()) == "population.EU27_2020"


@pytest.mark.parametrize("change, message", [
    ({"key": "A.NR.{sex}.{age}"}, "{geo}"),
    ({"key": "A.NR.{sex}.{geo}"}, "without placeholder: age"),
    ({"key": "A.NR.{sex}.{age}.{unit}.{geo}"}, "without breakdown codes: unit"),
    ({"key": "A.NR+PC.{sex}.{age}.{geo}"}, "fixed code"),
    ({"key": "A..{sex}.{age}.{geo}"}, "fixed code"),
    ({"key": "A.{sex}.{sex}.{age}.{geo}"}, "repeated"),
    ({"breakdown": {"sex": ["M", "F.X"], "age": ["TOTAL"]}}, "invalid codes"),
    ({"breakdown": {"sex": [], "age": ["TOTAL"]}}, "empty"),
    ({"geo_levels": ["nuts4"]}, "unknown geo levels"),
    ({"geo_levels": [], "geos": []}, "geo_levels, geos or both"),
    ({"geos": ["FR"]}, "unknown areas"),
    ({"topic": "economy"}, "unknown topic"),
    ({"frequency": "D"}, "unknown frequency"),
    ({"provider": "destatis"}, "unknown provider"),
    ({"concept": "Population"}, "snake_case"),
    ({"removed": date(2026, 10, 27)}, "together"),
    ({"colour": "red"}, "unknown keys"),
])
def test_invalid_entries_are_rejected(change: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message.replace("{", r"\{").replace("}", r"\}").replace("+", r"\+")):
        parse_entry({**BASE, **change})


def test_duplicate_concepts_are_rejected(tmp_path) -> None:
    text = CATALOG_FIXTURE.read_text(encoding="utf-8")
    duplicated = tmp_path / "series.toml"
    first_entry = "[[series]]" + text.split("[[series]]")[1]
    duplicated.write_text(text + "\n" + first_entry, encoding="utf-8")
    with pytest.raises(CatalogError, match="duplicate concepts: population"):
        load_catalog(duplicated)


def test_the_catalog_of_the_repository_is_valid() -> None:
    entries = load_catalog()
    regions = active_regions()
    assert len(entries) >= 50
    assert all(entry.geo_levels or entry.extra_geos for entry in entries)
    assert sum(entry.expected_series(regions) for entry in entries if entry.active) > 10_000
