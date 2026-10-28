"""Tests of the command-line checks of the entry point."""
import pytest

from main import main


def test_unknown_collector_on_the_command_line_is_rejected(tmp_path) -> None:
    with pytest.raises(SystemExit):
        main(["--collectors", "wikipedia_pageviews", "--output-dir", str(tmp_path)])
    assert not (tmp_path / "state").exists()


def test_unknown_collector_in_the_environment_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("COLLECTORS", "eurostat,unknown")
    assert main(["--output-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "state").exists()


def test_registry_collectors_run_without_network(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("COLLECTORS", raising=False)
    assert main(["--collectors", "series_catalog,regions", "--output-dir", str(tmp_path)]) == 0
    assert (tmp_path / "series_catalog").exists() and (tmp_path / "regions").exists()
