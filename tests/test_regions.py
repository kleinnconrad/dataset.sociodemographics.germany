"""Tests of the region registry."""
from collections import Counter
from datetime import date

import pytest

from conftest import REGIONS_FIXTURE
from regions import STATE_BY_NUTS1, RegistryError, active_regions, level_of, load_regions, parse_region, state_of

BASE = {"geo": "DE21", "name": "Oberbayern", "nuts": ["2021", "2024"], "added": date(2026, 10, 26)}


def test_level_parent_and_state_follow_from_the_code() -> None:
    region = parse_region(BASE)
    assert (region.level, region.parent, region.state) == ("nuts2", "DE2", "DE-BY")
    country = parse_region({**BASE, "geo": "DE", "name": "Deutschland"})
    assert (country.level, country.parent, country.state) == ("country", None, None)


def test_level_and_state_of_codes() -> None:
    assert level_of("DE212") == "nuts3" and state_of("DE212") == "DE-BY"
    assert level_of("DEA1") == "nuts2" and state_of("DEA1") == "DE-NW"
    assert level_of("EU27_2020") == "eu" and state_of("EU27_2020") is None
    assert level_of("FR1") is None


def test_removed_regions_are_not_active() -> None:
    assert [region.geo for region in active_regions(path=REGIONS_FIXTURE)] == [
        "DE", "DE1", "DE11", "DE111", "DE2", "DE21", "DE212"]
    assert [region.geo for region in active_regions({"nuts3"}, REGIONS_FIXTURE)] == ["DE111", "DE212"]


@pytest.mark.parametrize("change, message", [
    ({"geo": "FR1"}, "invalid NUTS code"),
    ({"geo": "DE2111"}, "invalid NUTS code"),
    ({"nuts": ["2013"]}, "nuts must be"),
    ({"name": " "}, "name"),
    ({"removed_reason": "merged"}, "together"),
    ({"level": "nuts2"}, "unknown keys"),
])
def test_invalid_regions_are_rejected(change: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_region({**BASE, **change})


def test_regions_without_parent_are_rejected(tmp_path) -> None:
    path = tmp_path / "regions.toml"
    path.write_text('[[region]]\ngeo = "DE21"\nname = "Oberbayern"\nnuts = ["2024"]\nadded = 2026-10-26\n',
                    encoding="utf-8")
    with pytest.raises(RegistryError, match="without their parent region: DE21"):
        load_regions(path)


def test_the_registry_of_the_repository_covers_all_german_nuts_regions() -> None:
    regions = load_regions()
    counts = Counter(region.level for region in regions if "2024" in region.nuts_versions)
    assert counts == {"country": 1, "nuts1": 16, "nuts2": 38, "nuts3": 400}
    assert {region.geo for region in regions if region.level == "nuts1"} == set(STATE_BY_NUTS1)
    assert len(set(STATE_BY_NUTS1.values())) == 16
    # The codes of Thüringen that NUTS 2024 replaced stay collected
    assert {"DEG04", "DEG0N", "DEG0P"} <= {region.geo for region in regions if region.active}
