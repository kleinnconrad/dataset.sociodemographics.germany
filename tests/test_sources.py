"""Tests of the source configuration and its lifecycle keys."""
from datetime import date
from pathlib import Path

import pytest

from sources import SourceHealth, SourceHealthStore, load_config

ENTRY = """\
[[outlet]]
id = "news_example"
name = "News Example"
homepage = "https://news.example/"
outlet_type = "national_daily"
owner_group = "Example Group"
paywall_model = "mixed"
feeds = ["https://news.example/rss"]
"""

PRESS = """\
[[press_source]]
id = "office"
name = "Landesamt"
homepage = "https://office.example/"
actor_type = "statistical_office"
jurisdiction = "DE-BY"
feeds = ["https://office.example/rss"]
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sources.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_lifecycle_keys_and_exclusions_are_read(tmp_path) -> None:
    text = (ENTRY + "added = 2026-10-28\nremoved = 2027-02-01\nremoved_reason = \"blocked\"\nkeep = true\n\n"
            "[[excluded]]\nid = \"faz\"\nname = \"FAZ\"\nhomepage = \"https://www.faz.net/\"\n"
            "status = \"tdm_reserved\"\nreason = \"TDMRep\"\nchecked = 2026-10-28\n")

    config = load_config(write(tmp_path, text))

    outlet = config.outlets[0]
    assert outlet.added == date(2026, 10, 28) and outlet.removed == date(2027, 2, 1)
    assert outlet.removed_reason == "blocked" and outlet.keep is True
    assert not outlet.active and config.active_sources == ()
    assert config.excluded[0].status == "tdm_reserved"
    assert not config.excluded[0].rechecked


def test_exclusion_with_a_temporary_reason_and_attributes_is_rechecked(tmp_path) -> None:
    text = ENTRY + ("\n[[excluded]]\nid = \"bib\"\nname = \"Bevölkerungsforschung\"\n"
                    "homepage = \"https://bib.example/\"\nkind = \"press_source\"\nactor_type = \"public_agency\"\n"
                    "jurisdiction = \"DE\"\nfeeds = [\"https://bib.example/rss\"]\nstatus = \"robots\"\n"
                    "reason = \"disallowed\"\nchecked = 2026-10-28\n\n[[excluded]]\nid = \"ekd\"\nname = \"EKD\"\n"
                    "homepage = \"https://ekd.example/\"\nstatus = \"terms\"\nreason = \"terms\"\n"
                    "checked = 2026-10-28\n")

    with_attributes, by_terms = load_config(write(tmp_path, text)).excluded

    assert with_attributes.rechecked and with_attributes.feeds == ("https://bib.example/rss",)
    assert with_attributes.actor_type == "public_agency" and with_attributes.jurisdiction == "DE"
    assert not by_terms.rechecked
    with pytest.raises(ValueError, match="actor_type"):
        load_config(write(tmp_path, text.replace('"public_agency"', '"agency"')))


@pytest.mark.parametrize("line", ['added = "2026-10-28"', "added = 2026-10-28T10:00:00Z"])
def test_lifecycle_dates_must_be_dates(tmp_path, line: str) -> None:
    with pytest.raises(ValueError, match="must be a date"):
        load_config(write(tmp_path, ENTRY + line + "\n"))


def test_unknown_exclusion_status_and_duplicate_ids_are_rejected(tmp_path) -> None:
    excluded = ("\n[[excluded]]\nid = \"{id}\"\nname = \"X\"\nhomepage = \"https://x.example/\"\n"
                "status = \"{status}\"\nreason = \"r\"\nchecked = 2026-10-28\n")
    with pytest.raises(ValueError, match="status"):
        load_config(write(tmp_path, ENTRY + excluded.format(id="x", status="unknown")))
    with pytest.raises(ValueError, match="Duplicate"):
        load_config(write(tmp_path, ENTRY + excluded.format(id="news_example", status="blocked")))


def test_topic_filter_defaults_by_kind_and_can_be_set(tmp_path) -> None:
    config = load_config(write(tmp_path, ENTRY + "\n" + PRESS))
    assert config.outlets[0].topic_filter is True and config.press_sources[0].topic_filter is False
    config = load_config(write(tmp_path, ENTRY + "topic_filter = false\n\n" + PRESS + "topic_filter = true\n"))
    assert config.outlets[0].topic_filter is False and config.press_sources[0].topic_filter is True
    with pytest.raises(ValueError, match="jurisdiction"):
        load_config(write(tmp_path, PRESS.replace('"DE-BY"', '"Bavaria"')))


def test_specialist_outlets_need_a_topic_of_the_vocabulary(tmp_path) -> None:
    specialist = ENTRY.replace('"national_daily"', '"specialist_press"')
    with pytest.raises(ValueError, match="topic must be set"):
        load_config(write(tmp_path, specialist))
    outlet = load_config(write(tmp_path, specialist + "topic = \"Migration and asylum\"\n")).outlets[0]
    assert outlet.topic == "Migration and asylum" and outlet.topic_filter is False
    with pytest.raises(ValueError, match="topic"):
        load_config(write(tmp_path, specialist + "topic = \"Migration\"\n"))
    with pytest.raises(ValueError, match="topic must be set"):
        load_config(write(tmp_path, ENTRY + "topic = \"Migration and asylum\"\n"))


def test_removed_sources_have_the_status_removed(tmp_path) -> None:
    config = load_config(write(tmp_path, ENTRY + "removed = 2027-02-01\n"))
    health = SourceHealthStore(tmp_path / "sources.json", {"news_example": SourceHealth(status="blocked")})

    assert health.status_of(config.outlets[0]) == "removed"


def test_broken_rule_and_retry(tmp_path) -> None:
    health = SourceHealthStore(tmp_path / "sources.json", {})
    today = date(2026, 10, 28)
    for _ in range(6):
        health.record_failure("news_example", today, "timeout")
    assert health.get("news_example").status == "active"
    assert health.record_failure("news_example", today, "timeout").status == "broken"
    assert not health.is_due("news_example", today)
    assert health.is_due("news_example", date(2026, 11, 4))
    health.record_success("news_example", date(2026, 11, 4), 3)
    assert health.get("news_example").status == "active"
