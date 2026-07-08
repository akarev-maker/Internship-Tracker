"""
util.py — shared helpers (HTTP session with retries, text sanitization).

Carried over from the Morning-Briefing project's hardening: a retry session so a
flaky moment doesn't drop a source, and sanitizers so untrusted posting text
can't inject markup or forge links into the digest email.
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


def strip_html(text, limit=None):
    """Remove HTML tags + squash whitespace from untrusted free text."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def md_escape(text):
    """Escape Markdown-control chars so untrusted text can't forge links/markup."""
    if not text:
        return ""
    out = str(text)
    for ch in ("\\", "`", "*", "_", "[", "]", "<", ">"):
        out = out.replace(ch, "\\" + ch)
    return out


def safe_url(url):
    """Allow only http(s) URLs — blocks javascript:/data: injection."""
    url = (url or "").strip()
    return url if url.startswith(("http://", "https://")) else ""
