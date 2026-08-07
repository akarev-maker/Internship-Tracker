"""
ats_boards.py — postings from curated company ATS boards (Greenhouse/Lever).

Public, keyless JSON APIs; one request per company per day via the shared
retry session. companies.toml lists the boards. Only internship titles that
match the security-career allowlist are kept (the company being on the
curated list is the security signal; the allowlist keeps out non-career
roles), normalized to the standard posting shape documented in sources.py.
These APIs do not publish application close dates, so deadline is always "".
"""

import hashlib
import logging
import re
import tomllib

from dateutil import parser as dateparser

from util import (SESSION, USER_AGENT, location_rank, safe_url, strip_html,
                  word_match)

logger = logging.getLogger("tracker.ats")

COMPANIES_PATH = "companies.toml"
GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"

# Security roles plus the classic entry-path roles into the field
# (the user's target: pen testing). Word-boundary matched — never substring.
CAREER_ALLOWLIST = (
    "security", "cyber", "cybersecurity", "pentest", "pen test", "penetration",
    "red team", "blue team", "appsec", "infosec", "soc analyst", "soc intern",
    "threat", "vulnerability", "incident", "forensic", "malware", "detection",
    "exploit", "identity", "iam", "grc", "network", "it intern",
    "information technology", "helpdesk", "help desk",
    "system administrator", "sysadmin", "technical support",
)


def relevant_intern_title(title):
    """Internship + security-career match. \\bintern(ship)?s?\\b avoids
    matching "internal"; the allowlist uses word_match so "soc" can't fire
    inside "Associate". "-ship" forms ("IT Internship") don't survive
    word_match's suffix group (s/es/ed/er/ers/ing only), so normalize
    "internship(s)" -> "intern" before the allowlist pass — that's a text
    fix, not a widening of word_match's shared suffix set."""
    low = title.lower()
    low = re.sub(r"\binternships?\b", "intern", low)
    if not re.search(r"\bintern(?:ship)?s?\b", low):
        return False
    return any(word_match(kw, low) for kw in CAREER_ALLOWLIST)


def load_companies(path=COMPANIES_PATH):
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        logger.warning("No usable %s (%s) — no ATS boards polled.", path, exc)
        return []
    boards = []
    for ats in ("greenhouse", "lever"):
        for entry in data.get(ats, []):
            if entry.get("name") and entry.get("slug"):
                boards.append({"ats": ats, "name": entry["name"],
                               "slug": entry["slug"]})
            else:
                logger.warning("Skipping malformed %s entry in %s: %r",
                               ats, path, entry)
    return boards


def _pid(ats, slug, job_id):
    return "ats:" + hashlib.sha256(
        f"{ats}:{slug}:{job_id}".encode("utf-8")).hexdigest()[:16]


def _to_epoch(value):
    if not value:
        return 0
    try:
        return int(dateparser.parse(str(value)).timestamp())
    except (ValueError, TypeError, OverflowError):
        return 0


def _posting(ats, slug, company, job_id, title, link, locations, date_posted):
    return {
        "id": _pid(ats, slug, job_id),
        "title": title,
        "company": company,
        "link": safe_url(link),
        "locations": locations,
        "location_str": ", ".join(locations) if locations else "Unspecified",
        "source": "Greenhouse" if ats == "greenhouse" else "Lever",
        "term": "",
        "date_posted": date_posted,
        "deadline": "",  # Greenhouse/Lever don't publish close dates
        "rank": location_rank(locations),
    }


def _parse_greenhouse(name, slug, data):
    out = []
    for job in data.get("jobs", []):
        job_id = job.get("id", "")
        if not job_id:
            continue
        title = strip_html(job.get("title", ""))
        if not relevant_intern_title(title):
            continue
        loc = (job.get("location") or {}).get("name", "")
        locations = [loc] if loc else []
        out.append(_posting("greenhouse", slug, name, job_id, title,
                            job.get("absolute_url", ""), locations,
                            _to_epoch(job.get("updated_at"))))
    return out


def _parse_lever(name, slug, data):
    out = []
    for job in data if isinstance(data, list) else []:
        job_id = job.get("id", "")
        if not job_id:
            continue
        title = strip_html(job.get("text", ""))
        if not relevant_intern_title(title):
            continue
        loc = ((job.get("categories") or {}).get("location")) or ""
        locations = [loc] if loc else []
        created = job.get("createdAt")
        date_posted = int(created / 1000) if isinstance(created, (int, float)) else 0
        out.append(_posting("lever", slug, name, job_id, title,
                            job.get("hostedUrl", ""), locations, date_posted))
    return out


def fetch_ats_postings(config_path=COMPANIES_PATH):
    postings = []
    for board in load_companies(config_path):
        url = (GREENHOUSE_URL if board["ats"] == "greenhouse"
               else LEVER_URL).format(slug=board["slug"])
        try:
            resp = SESSION.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            parse = _parse_greenhouse if board["ats"] == "greenhouse" else _parse_lever
            found = parse(board["name"], board["slug"], data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error fetching %s board '%s': %s",
                           board["ats"], board["name"], exc)
            continue
        postings.extend(found)
        logger.info("%s (%s): %d relevant intern posting(s)",
                    board["name"], board["ats"], len(found))
    return postings
