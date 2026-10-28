"""
URL normalization helpers used as comparison keys for articles and press releases.
"""
import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that only track the visitor and never change the page content
TRACKING_PARAM_PREFIXES = ("utm_", "mc_", "wt_", "at_", "pk_")
TRACKING_PARAMS = frozenset({"fbclid", "gclid", "igshid", "msclkid", "_ga", "ref", "xtor", "cid", "icid",
                             "ns_campaign", "ns_mchannel", "ns_source", "ns_linkname", "ns_fee"})


def _is_tracking_param(name: str) -> bool:
    """Returns True if a query parameter is used for tracking only.

    Args:
        name: Name of the query parameter.

    Returns:
        bool: True for tracking parameters such as ``utm_source`` or ``fbclid``.
    """
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PARAM_PREFIXES)


def domain_id(url: str) -> str:
    """Returns the lowercase host of a URL without a leading ``www.``.

    Args:
        url: An absolute URL.

    Returns:
        str: The host, e.g. ``tagesschau.de``.

    Raises:
        ValueError: If the URL has no host.
    """
    host = (urlsplit(url.strip()).hostname or "").lower()
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    return host[4:] if host.startswith("www.") else host


def normalize_url(url: str) -> str:
    """Returns a comparison key for a URL.

    The key uses https, drops ``www.``, default ports, the fragment and tracking
    parameters, strips a trailing slash from the path and sorts the remaining
    query parameters. It is used to compare URLs, not to fetch them.

    Args:
        url: An absolute URL.

    Returns:
        str: The normalized URL.

    Raises:
        ValueError: If the URL has no host or an invalid port.
    """
    parts = urlsplit(url.strip())
    host = domain_id(url)
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(key)
    ]
    return urlunsplit(("https", netloc, path, urlencode(sorted(query_pairs)), ""))


def url_id(url: str) -> str:
    """Returns a short stable id derived from the normalized URL.

    Args:
        url: An absolute URL.

    Returns:
        str: 16 hexadecimal digits.
    """
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def path_tokens(url: str) -> set[str]:
    """Returns the lowercase words of the path of a URL, split at slashes, hyphens and dots.

    Args:
        url: An absolute URL.

    Returns:
        set[str]: For example ``{"politik", "deutschland", "koalition"}``.
    """
    path = urlsplit(url).path.lower()
    for separator in ("-", "_", ".", "~", ","):
        path = path.replace(separator, "/")
    return {token for token in path.split("/") if token}
