"""Tests of the HTTP client and its fetch rules."""
import pytest

from web import (BlockedError, FetchError, NotFoundError, RateLimitError, RobotsDisallowedError, is_challenge,
                 is_rate_limited, parse_robots)

PAGE = "https://site.example/data/page-1"


def test_longest_matching_rule_wins() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /SiteGlobals/\nAllow: /SiteGlobals/Functions/RSSFeed\n"
                         "Crawl-delay: 30\n")
    assert rules.allows("/SiteGlobals/Functions/RSSFeed/feed.xml")
    assert not rules.allows("/SiteGlobals/Forms/search.html")
    assert rules.allows("/data/page.html")
    assert rules.crawl_delay == 30


def test_robots_rules_of_equal_length_prefer_allow_and_support_wildcards() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /page\nAllow: /page\nDisallow: /*.pdf$\nDisallow:\n")
    assert rules.allows("/page")
    assert not rules.allows("/files/report.pdf")
    assert rules.allows("/files/report.pdf?download=1")


def test_robots_group_of_the_agent_replaces_the_default_group() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /\n\nUser-agent: OtherBot\nUser-agent: germansociodemographicsdataset\n"
                         "Disallow: /private/\n")
    assert rules.allows("/data/")
    assert not rules.allows("/private/x")


def test_robots_txt_disallow_is_respected(fake_web) -> None:
    fake_web.add("https://site.example/robots.txt", "User-agent: *\nDisallow: /data/\n", content_type="text/plain")
    fake_web.add(PAGE, "<html></html>")

    with pytest.raises(RobotsDisallowedError):
        fake_web.client().get(PAGE)
    assert PAGE not in fake_web.requested


def test_missing_robots_txt_allows_everything(fake_web) -> None:
    fake_web.add(PAGE, "<html><p>Text</p></html>")

    response = fake_web.client().get(PAGE)

    assert response.status == 200
    assert "Text" in response.text


def test_robots_check_can_be_skipped_for_documented_apis(fake_web) -> None:
    fake_web.add("https://api.example/robots.txt", "User-agent: *\nDisallow: /\n", content_type="text/plain")
    fake_web.add("https://api.example/data.json", {"ok": True})

    assert fake_web.client().get("https://api.example/data.json", respect_robots=False).json() == {"ok": True}


@pytest.mark.parametrize("status", [401, 403])
def test_refusals_raise_blocked(fake_web, status: int) -> None:
    fake_web.add(PAGE, "Forbidden", status=status)

    with pytest.raises(BlockedError):
        fake_web.client().get(PAGE)


def test_exhausted_quota_raises_rate_limit_without_retry(fake_web) -> None:
    url = "https://api.example/repos/a/b"
    fake_web.add(url, {"message": "API rate limit exceeded"}, status=403,
                 headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1793000000"})

    with pytest.raises(RateLimitError):
        fake_web.client().get(url, respect_robots=False)
    assert fake_web.requested.count(url) == 1


def test_http_429_is_retried_and_then_raises_rate_limit(fake_web) -> None:
    url = "https://api.example/search"
    fake_web.add(url, "slow down", status=429, headers={"retry-after": "1"})

    with pytest.raises(RateLimitError):
        fake_web.client().get(url, respect_robots=False)
    assert fake_web.requested.count(url) == 3


def test_rate_limit_detection() -> None:
    assert is_rate_limited(429, {})
    assert is_rate_limited(403, {"x-ratelimit-remaining": "0"})
    assert is_rate_limited(403, {"retry-after": "60"})
    assert not is_rate_limited(403, {"x-ratelimit-remaining": "12"})


def test_cloudflare_challenge_raises_blocked(fake_web) -> None:
    fake_web.add(PAGE, "<html><title>Just a moment...</title></html>", status=503,
                 headers={"cf-mitigated": "challenge"})

    with pytest.raises(BlockedError):
        fake_web.client().get(PAGE)


def test_long_page_that_mentions_a_captcha_is_not_blocked() -> None:
    body = ("<html><body>" + "<p>An article about CAPTCHAs and g-recaptcha.</p>" * 1000 + "</body></html>").encode()
    assert not is_challenge(200, {"content-type": "text/html"}, body)


def test_not_found_raises_not_found(fake_web) -> None:
    with pytest.raises(NotFoundError):
        fake_web.client().get(PAGE)


def test_server_errors_are_retried(fake_web) -> None:
    fake_web.add(PAGE, "error", status=502)
    client = fake_web.client()

    with pytest.raises(FetchError) as excinfo:
        client.get(PAGE)

    assert excinfo.value.status == 502
    assert fake_web.requested.count(PAGE) == 3


def test_network_errors_become_fetch_errors(fake_web) -> None:
    fake_web.fail(PAGE)

    with pytest.raises(FetchError):
        fake_web.client().get(PAGE)


def test_robots_txt_sitemaps_and_tdm_statements_are_read() -> None:
    robots = parse_robots("# Text and data mining is reserved\nUser-agent: *\nDisallow: /a\n"
                          "Sitemap: https://site.example/sitemap.xml\nSitemap: https://site.example/sitemap.xml\n")

    assert robots.tdm_statement is True
    assert robots.sitemaps == ("https://site.example/sitemap.xml",)
    assert parse_robots("User-agent: *\nDisallow: /tdmrep-info\n# see TDMRep\n").tdm_statement is False


def test_permanent_redirects_are_reported(fake_web) -> None:
    fake_web.add(PAGE, "", status=301, headers={"location": "https://site.example/new"})
    fake_web.add("https://site.example/new", "<html></html>")
    fake_web.add("https://site.example/temp", "", status=302, headers={"location": "https://site.example/new"})
    client = fake_web.client()

    moved = client.get(PAGE)
    temporary = client.get("https://site.example/temp")

    assert moved.url == "https://site.example/new" and moved.moved_permanently
    assert temporary.redirects == (302,) and not temporary.moved_permanently


def test_unreachable_robots_txt_is_requested_once_per_run(fake_web) -> None:
    fake_web.fail("https://site.example/robots.txt")
    client = fake_web.client()

    for url in (PAGE, "https://site.example/data/page-2"):
        with pytest.raises(FetchError):
            client.get(url)

    assert fake_web.requested.count("https://site.example/robots.txt") == 1
    assert PAGE not in fake_web.requested


def test_tdmrep_rules_are_matched_by_path(fake_web) -> None:
    fake_web.add("https://site.example/.well-known/tdmrep.json",
                 [{"location": "/data/*", "tdm-reservation": 1}, {"location": "/", "tdm-reservation": 0}])
    client = fake_web.client()

    assert client.tdm_reserved(PAGE)
    assert not client.tdm_reserved("https://site.example/other/page-2")


def test_host_with_an_excessive_crawl_delay_is_not_fetched(fake_web) -> None:
    fake_web.add("https://site.example/robots.txt", "User-agent: *\nCrawl-delay: 180\n", content_type="text/plain")
    fake_web.add(PAGE, "<html></html>")

    with pytest.raises(RobotsDisallowedError):
        fake_web.client().get(PAGE)
    assert PAGE not in fake_web.requested


def test_crawl_delay_and_host_intervals_extend_the_wait(fake_web) -> None:
    fake_web.add("https://site.example/robots.txt", "User-agent: *\nCrawl-delay: 5\n", content_type="text/plain")
    fake_web.add(PAGE, "<html></html>")
    fake_web.add("https://api.example/a", {"ok": True})
    waits: list[float] = []
    client = fake_web.client()
    client.sleep = waits.append
    client.clock = lambda: 100.0
    client.set_host_interval("api.example", 3.0)

    client.get(PAGE)
    client.get(PAGE)
    client.get("https://api.example/a", respect_robots=False)
    client.get("https://api.example/a", respect_robots=False)

    assert waits == [pytest.approx(5.0), pytest.approx(5.0), pytest.approx(3.0)]
