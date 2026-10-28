"""Tests of the run summary of the notification workflow."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_summary.py"
_spec = importlib.util.spec_from_file_location("run_summary", SCRIPT)
assert _spec is not None and _spec.loader is not None
run_summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_summary)

ENTRY = {
    "started": "2026-10-27T01:07:00+00:00", "finished": "2026-10-27T01:19:00+00:00", "mode": "normal",
    "requests": 152,
    "collectors": {
        "series_catalog": {"status": "ok"},
        "ecb": {"status": "ok", "records": {"sociodemographic_series": 3},
                "counters": {"new": 0, "revised": 3, "values": 3, "not_found": 0, "failed": 0}},
        "eurostat": {"status": "ok", "records": {"sociodemographic_series": 120},
                     "counters": {"new": 100, "revised": 20, "values": 900, "unchanged_datasets": 40,
                                  "not_found": 1, "failed": 1, "not_found_entries": ["median_age"],
                                  "failed_entries": ["neet_rate: the response has 6 dimensions"]}},
    },
}


def write_log(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "run_log.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def test_summary_lists_collectors_and_series() -> None:
    text = run_summary.build_summary(ENTRY, "success", "https://github.com/run/1", "owner")

    assert text.startswith("@owner German sociodemographics pipeline: **success**")
    assert "duration 12 min" in text
    assert "| `series_catalog` | ok |  |" in text
    assert "| `eurostat` | ok | sociodemographic_series 120 |" in text
    assert ("**Statistical series:** 100 new and 23 revised series with 903 values; 40 catalog entries skipped "
            "because their dataset is unchanged.") in text
    assert "**Entries without data:** `median_age`" in text
    assert "**Failed entries:** neet_rate: the response has 6 dimensions" in text


def test_entry_of_an_earlier_run_is_ignored(tmp_path: Path) -> None:
    log = write_log(tmp_path, ENTRY)

    assert run_summary.last_entry(log) == ENTRY
    assert run_summary.last_entry(log, datetime(2026, 10, 28, tzinfo=timezone.utc)) is None
    assert run_summary.last_entry(tmp_path / "missing.jsonl") is None
    assert "wrote no run log entry" in run_summary.build_summary(None, "failure", None, None)


def test_main_writes_the_summary_file(tmp_path: Path) -> None:
    log = write_log(tmp_path, {"started": "2026-10-23T01:07:00+00:00", "mode": "normal", "collectors": {}}, ENTRY)
    output = tmp_path / "summary.md"

    assert run_summary.main(["--run-log", str(log), "--output", str(output), "--conclusion", "success",
                             "--not-before", "2026-10-27T01:06:30Z"]) == 0
    assert "German sociodemographics pipeline: **success**" in output.read_text(encoding="utf-8")


def test_summary_reports_feeds_and_the_cost_of_gemini() -> None:
    entry = {**ENTRY, "collectors": {"feeds": {"status": "ok", "records": {"news_items": 300}, "counters": {
        "items_new": 320, "sources_ok": 60, "annotated": 300, "beyond_cap": 20, "not_annotated": 0,
        "gemini_model": "gemini-2.5-flash",
        "gemini_usage": {"calls": 30, "prompt_tokens": 1_000_000, "output_tokens": 100_000, "thinking_tokens": 0}}}}}

    text = run_summary.build_summary(entry, "success", None, None)

    assert "**Feeds:** 320 new items from 60 sources; 300 annotated, 20 stored without labels." in text
    assert "estimated cost $0.55 at the list prices of gemini-2.5-flash" in text
    assert run_summary.gemini_cost("other-model", {"prompt_tokens": 1}) is None


def test_summary_reports_feeds_without_annotation() -> None:
    entry = {**ENTRY, "collectors": {"feeds": {"status": "ok", "counters": {
        "items_new": 12, "sources_ok": 5, "beyond_cap": 12, "annotation": "off: GEMINI_API_KEY is not set"}}}}

    assert "**Gemini:** off: GEMINI_API_KEY is not set." in run_summary.build_summary(entry, "success", None, None)
