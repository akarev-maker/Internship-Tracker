"""
sources.py — ingest internship postings from sources, normalized to one shape.

Every posting becomes:
    {
      id, title, company, link, locations, location_str, source,
      term, date_posted (unix int), deadline (ISO date str or ""), rank
    }

`deadline` is the whole point of this project. USAJOBS exposes a real
`ApplicationCloseDate`; the curated GitHub lists and the ATS boards
(Greenhouse/Lever — see `ats_boards.py` / `companies.toml`) don't publish
deadlines, so those postings are tracked by freshness.
"""

import logging
import os

from dateutil import parser as dateparser

import ats_boards

from util import SESSION, USER_AGENT, location_rank, safe_url, strip_html

logger = logging.getLogger("tracker.sources")

# --- Curated GitHub internship lists (high volume, usually no deadline) -----
# Note: Simplify serves one rolling listings.json across its season repos
# (the Summer2026 and Summer2027 URLs return the identical file), so pointing
# at one Simplify repo is enough; each listing carries its own `terms`.
GITHUB_SOURCES = [
    (
        "Simplify",
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    ),
    (
        "vanshb03",
        "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
    ),
]

# The GitHub lists are all-tech by construction (they are CS-internship
# lists), so no title filter: the profile weights + Gemini rank security
# roles to the top, and the general SWE/IT tail stays on the sheet as the
# long list to work through. Terms ARE filtered — the rolling file still
# carries seasons that have passed.
#
# A listing is kept if a term matches WANTED_TERMS, or if it names no dated
# term at all (many current postings just don't fill one in). When a new
# recruiting season starts, update this tuple.
WANTED_TERMS = ("Summer 2027",)
_UNLABELED = ("", "n/a", "none")


def term_is_wanted(terms):
    """terms: an iterable of term strings, or the joined string a record
    stores. Unlabeled postings pass; dated ones must name a wanted term."""
    if isinstance(terms, str):
        terms = terms.split(",")
    labeled = [t.strip() for t in terms
               if t and t.strip().lower() not in _UNLABELED]
    if not labeled:
        return True
    return any(t in WANTED_TERMS for t in labeled)


def stale_listing(rec):
    """True for a GitHub-list record whose season has passed — the purge
    predicate wired into tracker.py. ATS/USAJOBS records carry no term data
    and are never stale by term."""
    return rec.get("source") == "Simplify/GitHub" and not term_is_wanted(
        rec.get("term") or "")

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
            if not item.get("active"):
                continue
            terms = item.get("terms") or []
            if not term_is_wanted(terms):
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
                    "link": safe_url(item.get("url", "")),
                    "locations": locations,
                    "location_str": ", ".join(locations) if locations else "Unspecified",
                    "source": "Simplify/GitHub",
                    "term": ", ".join(strip_html(t) for t in terms),
                    "date_posted": item.get("date_posted") or 0,
                    "deadline": "",  # not exposed by these lists
                    "rank": location_rank(locations),
                }
            )
            added += 1
        logger.info("Fetched %d active internship(s) from %s", added, label)
    return postings


def fetch_usajobs():
    api_key = os.environ.get("USAJOBS_API_KEY")
    email = os.environ.get("USAJOBS_EMAIL")
    if not api_key or not email:
        logger.info("USAJOBS_API_KEY/USAJOBS_EMAIL not set — skipping USAJOBS.")
        return []
    # Named api_key, not key: the per-posting loop below binds its own `key`.
    headers = {"Host": "data.usajobs.gov", "User-Agent": email,
               "Authorization-Key": api_key}
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
                    "link": safe_url(d.get("PositionURI", "")),
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


if __name__ == "__main__":
    # `python sources.py` — inspect what the sources return, without touching
    # the store, the Sheet, or Gemini. Needs no secrets (USAJOBS is skipped
    # unless its key is set).
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    for _p in fetch_all_postings():
        print(f"[{_p['source']}] {_p['title']} — {_p['company']} "
              f"({_p['location_str']}) {_p['deadline'] or 'no deadline'}\n"
              f"    {_p['link']}")
