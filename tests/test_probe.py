"""Tests of the probe of candidate sources."""
import io
import re
import zipfile

from openpyxl import Workbook

from probe import probe_url, text_excerpts
from source_review import run_probes

PAGE = "https://stats.example/data/page.html"
ROBOTS = "https://stats.example/robots.txt"


def test_probe_reports_robots_tdm_and_links(fake_web) -> None:
    fake_web.add(ROBOTS, "User-agent: *\nDisallow: /private/\n# Reservation under § 44b UrhG\n",
                 content_type="text/plain")
    fake_web.add(PAGE, '<html><head><title>Daten</title><meta name="tdm-reservation" content="1"></head>'
                       '<body><p>Tabellen zum Download</p><a href="files/table.csv">CSV</a>'
                       '<a href="/about.html">About</a><script>var x;</script></body></html>')
    lines = probe_url(fake_web.client(), PAGE)
    assert "robots.txt: allows the URL; crawl delay none; plain-text TDM statement: yes" in lines
    assert "TDMRep file: no reservation" in lines
    assert "title: Daten" in lines and "meta tdm-reservation: 1" in lines
    assert lines[lines.index("links (2):") + 1] == "- CSV -> https://stats.example/data/files/table.csv"
    filtered = probe_url(fake_web.client(), PAGE, link_filter=r"about")
    assert "- About -> https://stats.example/about.html" in filtered and "links (1, filtered):" in filtered


def test_probe_does_not_fetch_disallowed_urls(fake_web) -> None:
    fake_web.add(ROBOTS, "User-agent: *\nDisallow: /data/\n", content_type="text/plain")
    lines = probe_url(fake_web.client(), PAGE)
    assert lines[-1] == "TDMRep file: no reservation"
    assert PAGE not in fake_web.requested


def test_probe_describes_files(fake_web) -> None:
    fake_web.add("https://stats.example/a.csv", "Kreis;Wert\n09162;1,5\n", content_type="text/csv")
    fake_web.add("https://stats.example/b.pdf", b"%PDF-1.7 rest", content_type="application/pdf")
    workbook = Workbook()
    workbook.active.title = "Inhalt"
    workbook.active.append(["Kirchenmitglieder am 31.12.2025"])
    sheet = workbook.create_sheet("csv-12613-03")
    sheet.append(["Jahr", "Woche", "Sterbefälle"])
    sheet.append([2026, 1, 21345])
    buffer = io.BytesIO()
    workbook.save(buffer)
    fake_web.add("https://stats.example/c.xlsx", buffer.getvalue(), content_type="application/octet-stream")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as members:
        members.writestr("data.csv", "a;b")
    fake_web.add("https://stats.example/e.zip", archive.getvalue(), content_type="application/zip")
    fake_web.add("https://stats.example/d.json", {"Status": {"Code": 0}, "Object": []})
    client = fake_web.client()
    assert probe_url(client, "https://stats.example/a.csv")[-2:] == ["Kreis;Wert", "09162;1,5"]
    assert probe_url(client, "https://stats.example/b.pdf")[-1] == "PDF document, version 1.7"
    assert probe_url(client, "https://stats.example/c.xlsx")[-6:] == [
        "xlsx workbook with 2 sheets: Inhalt, csv-12613-03", "sheet 'Inhalt':", "  Kirchenmitglieder am 31.12.2025",
        "sheet 'csv-12613-03':", "  Jahr | Woche | Sterbefälle", "  2026 | 1 | 21345"]
    assert probe_url(client, "https://stats.example/e.zip")[-1] == "zip archive with 1 members: data.csv"
    assert probe_url(client, "https://stats.example/d.json")[-2] == "JSON object with keys: Status, Object"
    assert probe_url(client, "https://stats.example/missing.csv")[-1] == "not found: not found (HTTP 404)"


def test_run_probes_writes_a_report(fake_web, tmp_path) -> None:
    fake_web.add("https://stats.example/a.csv", "x;y\n", content_type="text/csv")
    report = tmp_path / "probe.md"
    assert run_probes(["https://stats.example/a.csv"], None, report, web=fake_web.client()) == 0
    text = report.read_text(encoding="utf-8")
    assert text.startswith("# Source probe ") and "## https://stats.example/a.csv" in text


def test_text_excerpts_merge_overlapping_passages() -> None:
    text = "a" * 400 + " Datenlizenz Deutschland Namensnennung " + "b" * 600 + " Nutzung erlaubt " + "c" * 50
    excerpts = text_excerpts(text, re.compile("lizenz|namensnennung|nutzung", re.IGNORECASE))
    assert len(excerpts) == 2
    assert "Datenlizenz Deutschland Namensnennung" in excerpts[0] and excerpts[1].endswith("c" * 50)
