"""
HTTP access with the fetch rules of the pipeline.

Every request goes through ``WebClient``, which
- identifies the pipeline with a User-Agent that contains the repository URL,
- checks ``robots.txt`` per host with the longest-match rule of RFC 9309
  (``urllib.robotparser`` applies the first matching rule instead) and honors
  its crawl delay,
- keeps a minimum interval between requests to the same host,
- retries server errors, rate limits and network errors a few times,
- raises ``RateLimitError`` when an API reports an exhausted quota,
- detects bot protection (HTTP 401/403, challenge pages, CAPTCHAs) and raises
  ``BlockedError`` instead of trying to get around it,
- reads text-and-data-mining reservations (TDMRep: the ``tdm-reservation``
  header and ``/.well-known/tdmrep.json``).

The statistical sources of the dataset are documented APIs and data files,
which are called with ``respect_robots=False`` under their terms and rate
limits; feeds, sitemaps and article pages are website requests.

The module is named ``web`` because ``http`` would shadow the standard library.
"""
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional, Union
from urllib.parse import urlsplit

import httpx

from settings import USER_AGENT

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0
# Timeout of robots.txt and tdmrep.json requests; a host that does not answer them in time is skipped for the run
RULES_TIMEOUT_S = 10.0
DEFAULT_MIN_INTERVAL_S = 1.0
# Crawl delays of robots.txt are honored up to this limit. A host that requires a longer delay is not
# fetched at all, so that its delay is respected without stalling the run.
MAX_CRAWL_DELAY_S = 120.0
MAX_RETRIES = 2
RETRY_BACKOFF_S = (3.0, 10.0)
MAX_RETRY_AFTER_S = 60.0
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# Challenge pages of bot protection services. Only checked on error responses and on short HTML pages,
# so that a page that mentions a CAPTCHA does not count as blocked.
CHALLENGE_MARKERS = ("just a moment...", "cf-chl-", "challenge-platform", "px-captcha", "captcha-delivery",
                     "datadome", "attention required! | cloudflare", "g-recaptcha", "h-captcha")
SHORT_PAGE_BYTES = 20_000
# Token matched against the User-agent lines of robots.txt
ROBOTS_AGENT = USER_AGENT.split("/")[0]
# A text-and-data-mining reservation stated in natural language in robots.txt
TDM_STATEMENT_PATTERN = re.compile(r"§\s*44\s*b|data[\s-]*mining|(?<![a-z])tdm(?![a-z])", re.IGNORECASE)
PERMANENT_REDIRECTS = frozenset({301, 308})


class FetchError(Exception):
    """A request failed.

    Attributes:
        url (str): The requested URL.
        reason (str): Description of the failure.
        status (Optional[int]): HTTP status, if a response was received.
    """

    def __init__(self, url: str, reason: str, status: Optional[int] = None) -> None:
        super().__init__(f"{reason}: {url}")
        self.url = url
        self.reason = reason
        self.status = status


class NotFoundError(FetchError):
    """The resource does not exist (HTTP 404 or 410)."""


class BlockedError(FetchError):
    """The host refuses automated requests (HTTP 401/403, challenge page or CAPTCHA)."""


class RobotsDisallowedError(FetchError):
    """robots.txt does not allow the request for the pipeline's user agent."""


class RateLimitError(FetchError):
    """The API reports an exhausted quota or rate limit, also after the retries."""


@dataclass(frozen=True)
class Response:
    """A successful HTTP response.

    Attributes:
        url (str): Final URL after redirects.
        status (int): HTTP status.
        headers (Mapping[str, str]): Response headers with lower-case names.
        content (bytes): The body.
        encoding (Optional[str]): Character encoding detected by httpx.
        redirects (tuple[int, ...]): Status codes of the redirects that led to the final URL.
    """
    url: str
    status: int
    headers: Mapping[str, str]
    content: bytes
    encoding: Optional[str] = None
    redirects: tuple[int, ...] = ()

    @property
    def moved_permanently(self) -> bool:
        """True if the URL was reached only through permanent redirects (301 or 308)."""
        return bool(self.redirects) and all(status in PERMANENT_REDIRECTS for status in self.redirects)

    @property
    def content_type(self) -> str:
        """The media type without parameters, in lower case."""
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        """The body decoded as text; undecodable bytes are replaced."""
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    def json(self) -> Any:
        """Parses the body as JSON.

        Raises:
            ValueError: If the body is not valid JSON.
        """
        return json.loads(self.content)


def host_of(url: str) -> str:
    """Returns ``scheme://host[:port]`` of a URL."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _path_with_query(url: str) -> str:
    """Returns the path and query of a URL, as matched by robots.txt and TDMRep rules."""
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def is_rate_limited(status: int, headers: Mapping[str, str]) -> bool:
    """Tells whether a refusal is a rate limit rather than bot protection.

    GitHub answers an exhausted quota with HTTP 403 and ``x-ratelimit-remaining: 0``,
    and a secondary rate limit with HTTP 403 and ``retry-after``.

    Args:
        status: HTTP status.
        headers: Response headers with lower-case names.

    Returns:
        bool: True for HTTP 429, and for HTTP 403 with rate-limit headers.
    """
    if status == 429:
        return True
    return status == 403 and (headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers)


def is_challenge(status: int, headers: Mapping[str, str], content: bytes) -> bool:
    """Tells whether a response is a bot-protection challenge or a refusal.

    Args:
        status: HTTP status.
        headers: Response headers with lower-case names.
        content: The body.

    Returns:
        bool: True for HTTP 401 and 403, for Cloudflare challenges, and for
        error or short HTML pages that contain the marker of a challenge service.
    """
    if status in (401, 403):
        return True
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    content_type = headers.get("content-type", "").lower()
    if "html" not in content_type:
        return False
    if status < 400 and len(content) > SHORT_PAGE_BYTES:
        return False
    head = content[:SHORT_PAGE_BYTES].decode("utf-8", errors="ignore").lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


def _path_pattern_regex(pattern: str) -> re.Pattern[str]:
    """Converts a path pattern in robots.txt syntax (``*`` wildcard, ``$`` end anchor) to a regex."""
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    regex = "".join(".*" if char == "*" else re.escape(char) for char in body)
    return re.compile("^" + regex + ("$" if anchored_end else ""))


@dataclass(frozen=True)
class RobotsRules:
    """The rules of a robots.txt that apply to the pipeline's user agent (RFC 9309).

    Attributes:
        rules (tuple): ``(allow, pattern, regex)`` per allow or disallow line.
        crawl_delay (Optional[float]): Crawl delay in seconds, if the group sets one.
        sitemaps (tuple[str, ...]): URLs of the ``Sitemap`` lines.
        tdm_statement (bool): Whether the file states a text-and-data-mining
            reservation in natural language, e.g. in a comment.
    """
    rules: tuple[tuple[bool, str, re.Pattern[str]], ...] = ()
    crawl_delay: Optional[float] = None
    sitemaps: tuple[str, ...] = ()
    tdm_statement: bool = False

    def allows(self, path: str) -> bool:
        """Tells whether a path (with query) may be fetched.

        The longest matching pattern decides; if an allow and a disallow pattern
        of the same length match, the allow rule wins. Without a matching rule
        the path is allowed.

        Args:
            path: Path and query of the URL, starting with "/".

        Returns:
            bool: True if allowed.
        """
        best: Optional[tuple[int, bool]] = None
        for allow, pattern, regex in self.rules:
            if regex.match(path):
                candidate = (len(pattern), allow)
                if best is None or candidate > best:
                    best = candidate
        return True if best is None else best[1]


def parse_robots(text: str, agent: str = ROBOTS_AGENT) -> RobotsRules:
    """Parses a robots.txt and returns the rules for a user agent, following RFC 9309.

    The groups whose user-agent line equals the agent's product token
    (case-insensitive) are used; if there is none, the ``*`` groups are used.
    Rules of several matching groups are combined.

    Args:
        text: The robots.txt content.
        agent: Product token of the crawler.

    Returns:
        RobotsRules: The applicable rules and crawl delay, the sitemap URLs and
        whether the file states a text-and-data-mining reservation.
    """
    groups: list[tuple[set[str], list[tuple[bool, str]], Optional[float]]] = []
    agents: set[str] = set()
    rules: list[tuple[bool, str]] = []
    delay: Optional[float] = None
    sitemaps: list[str] = []
    in_rules = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "sitemap":
            if value:
                sitemaps.append(value)
        elif key == "user-agent":
            if in_rules:
                groups.append((agents, rules, delay))
                agents, rules, delay, in_rules = set(), [], None, False
            agents.add(value.lower())
        elif key in ("allow", "disallow") and agents:
            in_rules = True
            if value:
                rules.append((key == "allow", value))
        elif key == "crawl-delay" and agents:
            in_rules = True
            try:
                delay = float(value)
            except ValueError:
                pass
    if agents:
        groups.append((agents, rules, delay))
    selected = [group for group in groups if agent.lower() in group[0]] or [group for group in groups if "*" in group[0]]
    combined = tuple((allow, pattern, _path_pattern_regex(pattern)) for group in selected for allow, pattern in group[1])
    delays = [group[2] for group in selected if group[2] is not None]
    return RobotsRules(rules=combined, crawl_delay=max(delays) if delays else None,
                       sitemaps=tuple(dict.fromkeys(sitemaps)), tdm_statement=bool(TDM_STATEMENT_PATTERN.search(text)))


@dataclass
class WebClient:
    """HTTP client that applies the fetch rules of the pipeline.

    Attributes:
        user_agent (str): User-Agent header of all requests.
        min_interval_s (float): Minimum time between two requests to the same host.
        host_intervals (dict[str, float]): Longer or shorter intervals for
            single hosts, keyed by host name (e.g. ``data-api.ecb.europa.eu``).
        timeout_s (float): Timeout per request.
        transport (Optional[httpx.BaseTransport]): Transport for tests.
        sleep (Callable[[float], None]): Wait function; replaceable in tests.
        clock (Callable[[], float]): Monotonic clock; replaceable in tests.
        request_count (int): Number of requests sent, including robots.txt.
    """
    user_agent: str = USER_AGENT
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S
    host_intervals: dict[str, float] = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S
    transport: Optional[httpx.BaseTransport] = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    request_count: int = 0

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept-Language": "de-DE,de;q=0.9,en;q=0.5"},
            timeout=self.timeout_s,
            follow_redirects=True,
            transport=self.transport,
        )
        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, Optional[RobotsRules]] = {}
        self._robots_errors: dict[str, FetchError] = {}
        self._crawl_delays: dict[str, float] = {}
        self._tdm_rules: dict[str, list[tuple[re.Pattern[str], bool]]] = {}

    def __enter__(self) -> "WebClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Closes the connection pool."""
        self._client.close()

    # -- pacing -------------------------------------------------------------

    def set_host_interval(self, host_name: str, interval_s: float) -> None:
        """Sets the minimum interval between requests to a host, e.g. after reading an API key.

        Args:
            host_name: Host name, e.g. ``www.smard.de``.
            interval_s: Minimum interval in seconds.
        """
        self.host_intervals[host_name] = interval_s

    def _interval_for(self, url: str) -> float:
        """Returns the minimum interval between requests to the host of a URL."""
        host_name = urlsplit(url).hostname or ""
        interval = self.host_intervals.get(host_name, self.min_interval_s)
        return max(interval, self._crawl_delays.get(host_of(url), 0.0))

    def _wait_for_host(self, url: str) -> None:
        """Waits until the minimum interval since the previous request to the host has passed."""
        host = host_of(url)
        previous = self._last_request_at.get(host)
        if previous is not None:
            remaining = self._interval_for(url) - (self.clock() - previous)
            if remaining > 0:
                self.sleep(remaining)

    def _send(self, url: str, params: Optional[Mapping[str, Any]],
              headers: Optional[Mapping[str, Union[str, bytes]]],
              timeout_s: Optional[float] = None, data: Optional[Mapping[str, Any]] = None) -> httpx.Response:
        """Sends one request after waiting for the host interval: GET, or POST if form data is given.

        Args:
            url: The URL.
            params: Query parameters.
            headers: Additional request headers.
            timeout_s: Timeout of this request; None uses ``timeout_s`` of the client.
            data: Form fields of a POST request; None sends a GET request.

        Returns:
            httpx.Response: The response.
        """
        self._wait_for_host(url)
        self.request_count += 1
        options: dict[str, Any] = {"params": params, "headers": headers}
        if timeout_s is not None:
            options["timeout"] = timeout_s
        try:
            if data is None:
                return self._client.get(url, **options)
            return self._client.post(url, data=data, **options)
        finally:
            self._last_request_at[host_of(url)] = self.clock()

    # -- robots.txt ---------------------------------------------------------

    def _load_robots(self, host: str) -> Optional[RobotsRules]:
        """Fetches and parses the robots.txt of a host.

        Args:
            host: ``scheme://host``.

        Returns:
            Optional[RobotsRules]: The rules, or None if every path is allowed (no robots.txt).

        Raises:
            FetchError: If the robots.txt cannot be fetched because of a server or network error;
                RFC 9309 then requires to assume that everything is disallowed.
        """
        url = f"{host}/robots.txt"
        try:
            response = self._send(url, None, None, timeout_s=RULES_TIMEOUT_S)
        except httpx.TransportError as e:
            raise FetchError(url, f"robots.txt unreachable ({e.__class__.__name__})") from e
        if response.status_code >= 500:
            raise FetchError(url, "robots.txt returned a server error", response.status_code)
        if response.status_code >= 400:
            return None
        robots = parse_robots(response.text)
        if robots.crawl_delay:
            if robots.crawl_delay > MAX_CRAWL_DELAY_S:
                logger.warning("Crawl delay of %s is %s s, longer than %.0f s; the host is not fetched.",
                               host, robots.crawl_delay, MAX_CRAWL_DELAY_S)
            self._crawl_delays[host] = robots.crawl_delay
        return robots

    def allowed(self, url: str) -> bool:
        """Tells whether robots.txt allows the pipeline to fetch a URL.

        Args:
            url: The URL.

        Returns:
            bool: True if allowed; False also if the host's crawl delay exceeds MAX_CRAWL_DELAY_S.

        Raises:
            FetchError: If the robots.txt of the host cannot be fetched; the failure
                is kept for the rest of the run, so that the host is not requested again.
        """
        host = host_of(url)
        if host in self._robots_errors:
            error = self._robots_errors[host]
            raise FetchError(error.url, error.reason, error.status)
        if host not in self._robots:
            try:
                self._robots[host] = self._load_robots(host)
            except FetchError as e:
                self._robots_errors[host] = e
                raise
        if self._crawl_delays.get(host, 0.0) > MAX_CRAWL_DELAY_S:
            return False
        robots = self._robots[host]
        return robots is None or robots.allows(_path_with_query(url))

    def robots_rules(self, url: str) -> Optional[RobotsRules]:
        """Returns the robots.txt rules of the host of a URL, loading them on first use.

        Args:
            url: A URL of the host.

        Returns:
            Optional[RobotsRules]: The rules, or None if the host has no robots.txt.

        Raises:
            FetchError: If the robots.txt of the host cannot be fetched.
        """
        self.allowed(url)
        return self._robots.get(host_of(url))

    # -- TDM reservation ----------------------------------------------------

    def _load_tdm_rules(self, host: str) -> list[tuple[re.Pattern[str], bool]]:
        """Fetches the TDMRep rules of a host from ``/.well-known/tdmrep.json``.

        Args:
            host: ``scheme://host``.

        Returns:
            list: ``(location regex, reserved)`` pairs; empty if the host publishes no rules.
        """
        url = f"{host}/.well-known/tdmrep.json"
        try:
            response = self._send(url, None, None, timeout_s=RULES_TIMEOUT_S)
        except httpx.TransportError:
            return []
        if response.status_code != 200:
            return []
        try:
            data = response.json()
        except ValueError:
            return []
        rules = []
        for rule in data if isinstance(data, list) else []:
            if isinstance(rule, dict) and isinstance(rule.get("location"), str):
                reserved = str(rule.get("tdm-reservation", "0")).strip() == "1"
                rules.append((_path_pattern_regex(rule["location"]), reserved))
        return rules

    def tdm_reserved(self, url: str, response: Optional[Response] = None) -> bool:
        """Tells whether the rights holder reserved text and data mining for a URL.

        Args:
            url: The URL.
            response: The response for the URL, if already fetched; its
                ``tdm-reservation`` header is checked as well.

        Returns:
            bool: True if any applicable TDMRep rule or header declares a reservation.
        """
        if response is not None and response.headers.get("tdm-reservation", "").strip() == "1":
            return True
        host = host_of(url)
        if host not in self._tdm_rules:
            self._tdm_rules[host] = self._load_tdm_rules(host)
        path = _path_with_query(url)
        return any(reserved and pattern.match(path) for pattern, reserved in self._tdm_rules[host])

    # -- requests -----------------------------------------------------------

    def post(
        self,
        url: str,
        data: Mapping[str, Any],
        *,
        headers: Optional[Mapping[str, Union[str, bytes]]] = None,
        timeout_s: Optional[float] = None,
    ) -> Response:
        """Sends form data to a documented API with the fetch rules (no robots.txt check).

        Args:
            url: The URL of the API method.
            data: The form fields.
            headers: Additional request headers, e.g. credentials that the API expects in headers.
            timeout_s: Timeout of this request; None uses ``timeout_s`` of the client.

        Returns:
            Response: The successful response.

        Raises:
            RateLimitError, BlockedError, NotFoundError, FetchError: As for ``get``.
        """
        return self.get(url, headers=headers, respect_robots=False, timeout_s=timeout_s, data=data)

    def get(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, Union[str, bytes]]] = None,
        respect_robots: bool = True,
        timeout_s: Optional[float] = None,
        data: Optional[Mapping[str, Any]] = None,
    ) -> Response:
        """Fetches a URL with the fetch rules.

        Args:
            url: The URL.
            params: Query parameters.
            headers: Additional request headers, e.g. ``If-None-Match`` for a conditional request.
            respect_robots: False only for documented APIs whose terms allow automated access.
            timeout_s: Timeout of this request; None uses ``timeout_s`` of the client.
            data: Form fields; if given, the request is sent as POST (used by ``post``).

        Returns:
            Response: The successful response; a conditional request can return status 304
            with an empty body.

        Raises:
            RobotsDisallowedError: If robots.txt disallows the URL.
            RateLimitError: If the API reports an exhausted quota, also after the retries.
            BlockedError: If the host refuses automated requests.
            NotFoundError: For HTTP 404 and 410.
            FetchError: For other failures, after retries.
        """
        if respect_robots and not self.allowed(url):
            raise RobotsDisallowedError(url, "disallowed by robots.txt")
        attempt = 0
        while True:
            try:
                raw = self._send(url, params, headers, timeout_s=timeout_s, data=data)
            except httpx.TransportError as e:
                if attempt < MAX_RETRIES:
                    self.sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
                    attempt += 1
                    continue
                raise FetchError(url, f"network error ({e.__class__.__name__})") from e
            response_headers = {key.lower(): value for key, value in raw.headers.items()}
            if is_rate_limited(raw.status_code, response_headers):
                # An exhausted quota (remaining 0) resets only after up to an hour; retrying would not help
                if attempt < MAX_RETRIES and response_headers.get("x-ratelimit-remaining") != "0":
                    self.sleep(self._retry_wait(raw, attempt))
                    attempt += 1
                    continue
                raise RateLimitError(url, f"rate limited (HTTP {raw.status_code})", raw.status_code)
            if is_challenge(raw.status_code, response_headers, raw.content):
                raise BlockedError(url, f"blocked (HTTP {raw.status_code})", raw.status_code)
            if raw.status_code in (404, 410):
                raise NotFoundError(url, f"not found (HTTP {raw.status_code})", raw.status_code)
            if raw.status_code in RETRY_STATUS_CODES and attempt < MAX_RETRIES:
                self.sleep(self._retry_wait(raw, attempt))
                attempt += 1
                continue
            if raw.status_code >= 400:
                raise FetchError(url, f"HTTP {raw.status_code}", raw.status_code)
            return Response(url=str(raw.url), status=raw.status_code, headers=response_headers,
                            content=raw.content, encoding=raw.encoding,
                            redirects=tuple(step.status_code for step in raw.history))

    @staticmethod
    def _retry_wait(response: httpx.Response, attempt: int) -> float:
        """Returns the wait before a retry, from Retry-After or the backoff schedule."""
        retry_after = response.headers.get("retry-after", "").strip()
        if retry_after.isdigit():
            return min(float(retry_after), MAX_RETRY_AFTER_S)
        return RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)]
