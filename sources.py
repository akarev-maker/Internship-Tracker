"""
sources.py — ingest internship postings from sources, normalized to one shape.

Every posting becomes:
    {
      id, title, company, link, locations, location_str, source,
      term, date_posted (unix int), deadline (ISO date str or ""), rank
    }

`deadline` is the whole point of this project. USAJOBS exposes a real
`ApplicationCloseDate`; the curated GitHub lists usually don't (deadline ""),
so those are tracked by freshness until a deadline-bearing source is added
(company ATS pages are the phase-2 target).
"""

import logging
import os

from dateutil import parser as dateparser

import ats_boards

from util import SESSION, USER_AGENT, location_rank, strip_html

logger = logging.getLogger("tracker.sources")

# --- Curated GitHub internship lists (high volume, usually no deadline) -----
GITHUB_SOURCES = [
    (
        "Summer 2026",
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    ),
    (
        "Summer 2027",
        "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
    ),
]

SECURITY_KEYWORDS = (
    "security", "cyber", "penetration", "pentest", "pen test", "red team",
    "appsec", "infosec", "soc analyst", "malware", "vulnerability",
    "incident response", "threat",
)

# --- USAJOBS (federal; real deadlines) --------------------------------------
USAJOBS_URL = "https://data.usajobs.gov/api/search"
USAJOBS_QUERIES = [
    ("penetration tester intern", None),
    ("cybersecurity intern", None),
    ("information security intern", None),
    ("cybersecurity student trainee", None),
    ("information technology student trainee", "Massachusetts"),
]
USAJOBS_INTERN_HINTS = ("intern", "student trainee", "pathways")


def _is_security_role(title):
    return any(k in title.lower() for k in SECURITY_KEYWORDS)


def _to_iso_date(value):
    if not value:
        return ""
    try:
        return dateparser.parse(value).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return ""


def fetch_github_lists():
    postings, seen = [], set()
    for label, url in GITHUB_SOURCES:
        try:
            resp = SESSION.get(url, headers={"User-Agent": USER_AGENT}, timeout=40)
            resp.raise_for_status()
            listings = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error fetching '%s': %s", label, exc)
            continue
        added = 0
        for item in listings:
            if not item.get("active") or not _is_security_role(item.get("title", "")):
                continue
            key = item.get("url") or item.get("id")
            if not key or key in seen:
                continue
            seen.add(key)
            locations = item.get("locations") or []
            postings.append(
                {
                    "id": key,
                    "title": strip_html(item.get("title", "")),
                    "company": strip_html(item.get("company_name") or ""),
                    "link": item.get("url", ""),
                    "locations": locations,
                    "location_str": ", ".join(locations) if locations else "Unspecified",
                    "source": "Simplify/GitHub",
                    "term": (item.get("terms") or [label])[0],
                    "date_posted": item.get("date_posted") or 0,
                    "deadline": "",  # not exposed by these lists
                    "rank": location_rank(locations),
                }
            )
            added += 1
        logger.info("Fetched %d security internship(s) from %s", added, label)
    return postings


def fetch_usajobs():
    key = os.environ.get("USAJOBS_API_KEY")
    email = os.environ.get("USAJOBS_EMAIL")
    if not key or not email:
        logger.info("USAJOBS_API_KEY/USAJOBS_EMAIL not set — skipping USAJOBS.")
        return []
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    postings, seen = [], set()
    for keyword, location in USAJOBS_QUERIES:
        params = {"Keyword": keyword, "ResultsPerPage": 25}
        if location:
            params["LocationName"] = location
        try:
            resp = SESSION.get(USAJOBS_URL, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            items = resp.json().get("SearchResult", {}).get("SearchResultItems", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error fetching USAJOBS '%s': %s", keyword, exc)
            continue
        added = 0
        for item in items:
            d = item.get("MatchedObjectDescriptor", {})
            title = d.get("PositionTitle", "")
            hp = [str(p).lower() for p in ((d.get("UserArea", {}) or {}).get("Details", {}) or {}).get("HiringPath", [])]
            is_intern = any(h in title.lower() for h in USAJOBS_INTERN_HINTS) or any(
                p in ("student", "internship", "intern") for p in hp
            )
            if not is_intern:
                continue
            key = d.get("PositionURI") or d.get("PositionID", "")
            if not key or key in seen:
                continue
            seen.add(key)
            locations = [
                loc.get("LocationName", "")
                for loc in d.get("PositionLocation", [])
                if loc.get("LocationName")
            ]
            postings.append(
                {
                    "id": key,
                    "title": strip_html(title),
                    "company": strip_html(d.get("OrganizationName") or ""),
                    "link": d.get("PositionURI", ""),
                    "locations": locations,
                    "location_str": ", ".join(locations) if locations else "Unspecified",
                    "source": "USAJOBS",
                    "term": "Federal",
                    "date_posted": 0,
                    "deadline": _to_iso_date(d.get("ApplicationCloseDate")),
                    "rank": location_rank(locations),
                }
            )
            added += 1
        logger.info("USAJOBS '%s': %d intern posting(s)", keyword, added)
    return postings


def _safe(fetcher, label):
    try:
        return fetcher()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Source '%s' failed entirely: %s", label, exc)
        return []


def fetch_all_postings():
    """All sources merged and deduped by id (first source wins)."""
    postings, seen = [], set()
    for src in (_safe(fetch_usajobs, "USAJOBS"),
                _safe(fetch_github_lists, "GitHub lists"),
                _safe(ats_boards.fetch_ats_postings, "ATS boards")):
        for p in src:
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            postings.append(p)
    with_deadline = sum(1 for p in postings if p["deadline"])
    logger.info("Total %d posting(s), %d with a deadline", len(postings), with_deadline)
    return postings
