"""
util.py — shared helpers (HTTP session with retries, matching, sanitization).

Everything here treats posting data as untrusted: it arrives from third-party
job feeds and ATS boards that anyone can publish to. `strip_html` and
`safe_url` are applied at ingestion so no posting can smuggle markup into the
Sheet or a `javascript:`/`data:` URL into the Link column.

A retry session keeps a flaky moment from silently dropping a whole source.
"""

import re

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # noqa: BLE001
    Retry = None

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _make_session():
    session = requests.Session()
    if Retry is not None:
        retry = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return session


SESSION = _make_session()


def word_match(keyword, text_lower):
    """Word-boundary keyword match tolerating common suffixes, so
    "penetration test" hits "Penetration Testing" but "api" never hits
    "Rapid7" and "soc" never hits "Associate". Caller lowercases the text."""
    return re.search(rf"\b{re.escape(keyword)}(?:s|es|ed|er|ers|ing)?\b",
                     text_lower) is not None


def location_rank(locations):
    """0 = Massachusetts, 1 = remote, 2 = elsewhere (lower sorts first)."""
    joined = " ".join(locations).lower()
    if any(c in joined for c in ("massachusetts", "boston", "cambridge")):
        return 0
    for loc in locations:
        tokens = [t.strip().lower() for t in loc.replace("/", ",").split(",")]
        if "ma" in tokens:
            return 0
    if "remote" in joined:
        return 1
    return 2


def strip_html(text, limit=None):
    """Remove HTML tags + squash whitespace from untrusted free text."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def safe_url(url):
    """Allow only http(s) URLs — blocks javascript:/data: injection."""
    url = (url or "").strip()
    return url if url.startswith(("http://", "https://")) else ""
