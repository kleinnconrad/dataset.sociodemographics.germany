"""Shared test helpers: a fake web served through httpx.MockTransport and a factory for run contexts."""
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest

from collectors.base import RunContext
from settings import Settings
from web import WebClient

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CATALOG_FIXTURE = FIXTURES_DIR / "config" / "series.toml"
REGIONS_FIXTURE = FIXTURES_DIR / "config" / "regions.toml"
PROVIDERS_FIXTURE = FIXTURES_DIR / "config" / "providers.toml"
SOURCES_FIXTURE = FIXTURES_DIR / "config" / "sources.toml"
RUN_STARTED = datetime(2026, 10, 27, 1, 7, tzinfo=timezone.utc)

Body = Union[bytes, str, dict, list]


class FakeWeb:
    """Serves prepared responses and records the requested URLs.

    A route matches the full request URL first, then the URL without its query.
    Unknown URLs, including robots.txt and tdmrep.json, return 404.
    """

    def __init__(self) -> None:
        self.routes: dict[str, httpx.Response] = {}
        self.requested: list[str] = []
        self.failing: set[str] = set()

    def add(self, url: str, body: Body, status: int = 200, content_type: Optional[str] = None,
            headers: Optional[dict[str, str]] = None) -> None:
        """Serves ``body`` for ``url``; dicts and lists are served as JSON."""
        if isinstance(body, (dict, list)):
            content = json.dumps(body).encode("utf-8")
            content_type = content_type or "application/json"
        elif isinstance(body, str):
            content = body.encode("utf-8")
        else:
            content = body
        all_headers = {"content-type": content_type or "text/html; charset=utf-8", **(headers or {})}
        self.routes[url] = httpx.Response(status, content=content, headers=all_headers)

    def add_fixture(self, url: str, fixture: str, content_type: Optional[str] = None) -> None:
        """Serves a file from tests/fixtures for ``url``."""
        path = FIXTURES_DIR / fixture
        if content_type is None:
            content_type = {".json": "application/json", ".xml": "application/xml", ".csv": "text/csv",
                            ".txt": "text/plain"}.get(path.suffix)
        self.add(url, path.read_bytes(), content_type=content_type)

    def fail(self, url: str) -> None:
        """Makes requests to ``url`` raise a connection error."""
        self.failing.add(url)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        parts = urlsplit(url)
        without_query = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if url in self.failing or without_query in self.failing:
            raise httpx.ConnectError(f"cannot reach {url}", request=request)
        response = self.routes.get(url) or self.routes.get(without_query)
        if response is None:
            return httpx.Response(404, content=b"Not found", headers={"content-type": "text/plain"})
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    def client(self) -> WebClient:
        """Returns a WebClient that uses this fake web and does not wait."""
        return WebClient(transport=httpx.MockTransport(self.handler), sleep=lambda seconds: None)


@pytest.fixture
def fake_web() -> FakeWeb:
    """A fresh fake web per test."""
    return FakeWeb()


@pytest.fixture
def make_ctx(tmp_path: Path, fake_web: FakeWeb) -> Callable[..., RunContext]:
    """Returns a factory for run contexts that write to a temporary folder.

    The factory accepts setting overrides as keyword arguments, plus ``dry_run``,
    ``backfill`` and ``started``.
    """
    def factory(dry_run: bool = False, backfill: bool = False, started: datetime = RUN_STARTED,
                **overrides: Any) -> RunContext:
        settings = replace(Settings(), output_dir=tmp_path / "data", **overrides)
        return RunContext(settings, fake_web.client(), started, dry_run=dry_run, clock=lambda: started,
                          backfill=backfill)
    return factory


def read_dataset(output_dir: Path, dataset: str) -> list[dict[str, Any]]:
    """Returns all records of a dataset written below ``output_dir``."""
    records: list[dict[str, Any]] = []
    for path in sorted((output_dir / dataset).glob("year=*/month=*/*.json")):
        records.extend(json.loads(path.read_text(encoding="utf-8")))
    return records
