"""
Checks candidate sources with the fetch rules and describes what they publish.

The probe is the tool for the rule that every new source is checked before it
is added (CLAUDE.md, sections 8 and 9): for every URL it reports whether
robots.txt allows the request for the pipeline's user agent, the crawl delay,
text-and-data-mining reservations (TDMRep file, header, meta tag and plain
statements in robots.txt), the response (status, redirects, type, size, last
modification) and a description of the content: the title, text and links of
an HTML page, the first lines of a CSV or text file, the keys of a JSON
document, or the members of an archive (xlsx, zip). Pages are fetched like
every website request of the pipeline; a URL that robots.txt disallows is
not fetched.

The source review runs the probe when the workflow ``source_review.yml`` is
started with URLs in the input ``probe``; the description goes to the job
summary, so that sources can be checked live from GitHub Actions.
"""
import io
import json
import re
import zipfile
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin

from web import BlockedError, FetchError, NotFoundError, Response, RobotsDisallowedError, WebClient

MAX_TEXT_CHARS = 2_000
MAX_LINKS = 120
MAX_LINES = 15
MAX_LINE_CHARS = 300
FILE_LINK_PATTERN = re.compile(r"\.(csv|xlsx?|ods|zip|json|pdf|txt|xml)(\?|#|$)", re.IGNORECASE)


class _PageParser(HTMLParser):
    """Collects the title, the visible text, the links and the TDM meta tag of an HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.tdm_meta: Optional[str] = None
        self.robots_meta: Optional[str] = None
        self._skip = 0
        self._in_title = False
        self._href: Optional[str] = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a" and values.get("href"):
            self._href = values["href"]
            self._link_text = []
        elif tag == "meta":
            name = values.get("name", "").lower()
            if name == "tdm-reservation":
                self.tdm_meta = values.get("content", "")
            elif name == "robots":
                self.robots_meta = values.get("content", "")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._href is not None:
            self.links.append((" ".join(" ".join(self._link_text).split()), self._href))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self.text.append(data)
        if self._href is not None:
            self._link_text.append(data)


def describe_html(response: Response, link_filter: Optional[re.Pattern[str]]) -> list[str]:
    """Describes an HTML page: title, robots and TDM meta tags, text excerpt and links.

    Args:
        response: The page.
        link_filter: Only links whose address or text matches are listed; None lists links to files
            first and then the other links.

    Returns:
        list[str]: Lines of the description.
    """
    parser = _PageParser()
    parser.feed(response.text)
    text = " ".join(" ".join(parser.text).split())
    lines = [f"title: {' '.join(parser.title.split())}"]
    if parser.tdm_meta is not None:
        lines.append(f"meta tdm-reservation: {parser.tdm_meta}")
    if parser.robots_meta is not None:
        lines.append(f"meta robots: {parser.robots_meta}")
    lines.append(f"text ({len(text):,} characters): {text[:MAX_TEXT_CHARS]}")
    links = [(label, urljoin(response.url, href)) for label, href in parser.links
             if not href.startswith(("javascript:", "mailto:", "#"))]
    if link_filter is not None:
        links = [(label, href) for label, href in links if link_filter.search(href) or link_filter.search(label)]
    else:
        links.sort(key=lambda link: not FILE_LINK_PATTERN.search(link[1]))
    unique = list(dict.fromkeys(links))
    lines.append(f"links ({len(unique)}{', filtered' if link_filter is not None else ''}):")
    lines.extend(f"- {label[:100]} -> {href}" for label, href in unique[:MAX_LINKS])
    return lines


def describe_content(response: Response, link_filter: Optional[re.Pattern[str]] = None) -> list[str]:
    """Describes the content of a response according to its type.

    Args:
        response: The response.
        link_filter: Filter of the links of an HTML page.

    Returns:
        list[str]: Lines of the description.
    """
    content = response.content
    kind = response.content_type
    if content[:4] == b"%PDF":
        return [f"PDF document, version {content[5:8].decode('ascii', errors='replace')}"]
    if content[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
        except zipfile.BadZipFile:
            return ["damaged zip archive"]
        sheets = [name for name in names if name.startswith("xl/worksheets/")]
        label = "xlsx workbook" if sheets else "zip archive"
        return [f"{label} with {len(names)} members: " + ", ".join(names[:40])]
    if "html" in kind or content.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        return describe_html(response, link_filter)
    if "json" in kind:
        try:
            data = json.loads(content)
        except ValueError:
            return ["invalid JSON"]
        if isinstance(data, dict):
            return [f"JSON object with keys: {', '.join(list(data)[:40])}", json.dumps(data, ensure_ascii=False)[:MAX_TEXT_CHARS]]
        return [f"JSON list with {len(data) if isinstance(data, list) else 0} items",
                json.dumps(data, ensure_ascii=False)[:MAX_TEXT_CHARS]]
    text = response.text
    lines = text.splitlines()
    return [f"text with {len(lines):,} lines; the first lines:"] + [line[:MAX_LINE_CHARS] for line in lines[:MAX_LINES]]


def probe_url(web: WebClient, url: str, link_filter: Optional[str] = None) -> list[str]:
    """Checks a URL with the fetch rules and describes the response.

    Args:
        web: The HTTP client.
        url: The URL.
        link_filter: Regular expression for the links of an HTML page to list.

    Returns:
        list[str]: Lines of the report.
    """
    lines = [f"URL: {url}"]
    try:
        rules = web.robots_rules(url)
        allowed = web.allowed(url)
    except FetchError as e:
        return lines + [f"robots.txt not available ({e.reason}); the URL is not fetched"]
    if rules is None:
        lines.append("robots.txt: none (every path allowed)")
    else:
        lines.append(f"robots.txt: {'allows' if allowed else 'disallows'} the URL; crawl delay "
                     f"{rules.crawl_delay or 'none'}; plain-text TDM statement: {'yes' if rules.tdm_statement else 'no'}")
    reserved = web.tdm_reserved(url)
    lines.append(f"TDMRep file: {'reserves' if reserved else 'no reservation'}")
    if not allowed:
        return lines
    try:
        response = web.get(url)
    except RobotsDisallowedError:
        return lines + ["not fetched: disallowed by robots.txt"]
    except BlockedError as e:
        return lines + [f"blocked: {e.reason} (bot protection or refusal)"]
    except NotFoundError as e:
        return lines + [f"not found: {e.reason}"]
    except FetchError as e:
        return lines + [f"failed: {e.reason}"]
    if web.tdm_reserved(url, response):
        lines.append("TDM reservation in the response header")
    headers = response.headers
    lines.append(f"response: HTTP {response.status}, final URL {response.url}"
                 + (f", redirects {list(response.redirects)}" if response.redirects else "")
                 + f", type {response.content_type or 'none'}, {len(response.content):,} bytes"
                 + (f", last modified {headers['last-modified']}" if headers.get("last-modified") else "")
                 + (f", etag {headers['etag']}" if headers.get("etag") else ""))
    pattern = re.compile(link_filter, re.IGNORECASE) if link_filter else None
    return lines + describe_content(response, pattern)
