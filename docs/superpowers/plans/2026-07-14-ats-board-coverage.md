# ATS Board Coverage + USAJOBS Tuning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new posting source — curated security-company ATS boards (Greenhouse/Lever public JSON) filtered to security-career intern roles — and widen the USAJOBS queries, so the tracker's pool stops being 19 postings from one feed.

**Architecture:** A new `ats_boards.py` module loads `companies.toml` (curated `{name, slug}` board entries), fetches each board's public JSON once per run through the existing retry session with per-board error isolation, filters titles to internships matching a security-career allowlist (word-boundary matching shared with `scoring.keyword_fit` via a new `util.word_match` helper), and normalizes to the standard posting shape. `sources.fetch_all_postings` gains one more source; `USAJOBS_QUERIES` goes nationwide for cybersecurity and adds an information-security query.

**Tech Stack:** Python 3.11, `requests` (existing `util.SESSION`), `tomllib`, `python-dateutil` (all already in requirements.txt — no new dependencies).

**Spec:** `docs/superpowers/specs/2026-07-14-ats-board-coverage-design.md`

## Global Constraints

- No new pip dependencies.
- Tests are network-free (`python3 -m pytest` from repo root); every test file starts with `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` (no conftest.py in this repo).
- Posting shape (must match `sources.py`'s other sources exactly): `id, title, company, link, locations, location_str, source, term, date_posted (unix int), deadline ("" — these APIs don't publish close dates), rank`.
- ATS posting ids: `"ats:" + sha256(f"{ats}:{slug}:{job_id}")[:16]` — stable across runs.
- Inclusion rule (verbatim from spec): title is an internship AND matches the security-career allowlist; **word-boundary matching, never bare substring** (`soc` must not match "Associate"; `it` must not match everything).
- Per-board error isolation: one failing board logs a warning with the company name and the rest continue. Missing/invalid `companies.toml` → warn + contribute nothing, never crash.
- Suite baseline before Task 1: **42 passed**. Expected after each task: T1 → 44, T2 → 52, T3 → 52, T4 → 55.
- Current HEAD at plan time: `fd6be99` on `main`. **Create a feature branch first** (Task 0).

---

### Task 0: Feature branch

**Files:** none (branch-only)

**Interfaces:**
- Produces: branch `feat/ats-board-coverage` off current `main`; all later tasks commit here.

- [ ] **Step 1: Verify clean baseline and branch**

```bash
git status --short   # expect empty
python3 -m pytest -q # expect 42 passed
git checkout -b feat/ats-board-coverage
```

---

### Task 1: `util.word_match` shared helper + move `location_rank` into `util.py`

Two shared-helper moves that unblock Task 2 without circular imports: the word-boundary matcher (currently inlined in `scoring.keyword_fit`) and `location_rank` (currently in `sources.py`, needed by `ats_boards.py`, which `sources.py` will import — moving it to `util.py` breaks the cycle).

**Files:**
- Modify: `util.py` (add `word_match`, add `location_rank`)
- Modify: `scoring.py` (use `util.word_match` in `keyword_fit`)
- Modify: `sources.py` (delete `location_rank`, import it from `util`)
- Test: `tests/test_tracker.py` (add 2 `word_match` tests; existing `test_location_rank` keeps passing unchanged)

**Interfaces:**
- Consumes: existing `scoring.keyword_fit` regex idiom.
- Produces: `util.word_match(keyword: str, text_lower: str) -> bool` (word-boundary, tolerates suffixes s/es/ed/er/ers/ing; caller lowercases text) and `util.location_rank(locations: list[str]) -> int` (0=MA, 1=remote, 2=else — moved verbatim). `sources.location_rank` keeps working as an imported name (tests reference it).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracker.py` (in the `# --- ranking inputs + sanitization ---` section):

```python
def test_word_match_boundaries_and_suffixes():
    assert util.word_match("penetration test", "penetration testing intern")
    assert util.word_match("api", "rest apis intern")
    assert not util.word_match("api", "security intern at rapid7")
    assert not util.word_match("soc", "associate product manager")


def test_location_rank_lives_in_util():
    assert util.location_rank(["Boston, MA"]) == 0
    assert util.location_rank(["Remote"]) == 1
    assert util.location_rank(["Austin, TX"]) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tracker.py -q`
Expected: 2 FAIL/ERROR — `AttributeError: module 'util' has no attribute 'word_match'` (and `location_rank`).

- [ ] **Step 3: Implement**

In `util.py`, add after the `SESSION = _make_session()` line:

```python
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
```

In `sources.py`: delete the `location_rank` function (lines 58–69) and change the util import to:

```python
from util import SESSION, USER_AGENT, location_rank, strip_html
```

(`location_rank` stays reachable as `sources.location_rank` via this import — `tests/test_tracker.py::test_location_rank` and `scoring.py` call sites are unaffected.)

In `scoring.py`, change `keyword_fit`'s matching to use the helper — replace:

```python
    # Word-boundary match, tolerating common suffixes so "penetration test"
    # still hits "Penetration Testing" (but "api" never hits "Rapid7").
    matched = [(kw, w) for kw, w in weights.items()
               if re.search(rf"\b{re.escape(kw)}(?:s|es|ed|er|ers|ing)?\b", low)]
```

with:

```python
    matched = [(kw, w) for kw, w in weights.items() if word_match(kw, low)]
```

and add `from util import word_match` to `scoring.py`'s imports; remove `import re` from `scoring.py` if nothing else in the file uses it (check — after this change nothing does).

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q`
Expected: `44 passed` (42 + 2; all existing keyword_fit/location tests still green — the behavior is identical).

- [ ] **Step 5: Commit**

```bash
git add util.py scoring.py sources.py tests/test_tracker.py
git commit -m "refactor: shared word_match + location_rank in util (unblocks ATS source)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `ats_boards.py` — parsing, inclusion rule, normalization, error isolation

**Files:**
- Create: `ats_boards.py`
- Test: `tests/test_ats_boards.py` (new)

**Interfaces:**
- Consumes: `util.SESSION`, `util.USER_AGENT`, `util.strip_html`, `util.word_match`, `util.location_rank` (Task 1).
- Produces: `ats_boards.fetch_ats_postings(config_path="companies.toml") -> list[dict]` (standard posting shape), `ats_boards.load_companies(path) -> list[dict]` (`{"ats","name","slug"}`), `ats_boards.relevant_intern_title(title: str) -> bool`, constants `COMPANIES_PATH`, `CAREER_ALLOWLIST`. Task 4 calls `fetch_ats_postings()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ats_boards.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ats_boards  # noqa: E402


GH_DATA = {"jobs": [
    {"id": 111, "title": "Security Engineer Intern", "absolute_url": "https://x/gh/111",
     "location": {"name": "Boston, MA"}, "updated_at": "2026-07-01T12:00:00-04:00"},
    {"id": 222, "title": "Penetration Testing Internship", "absolute_url": "https://x/gh/222",
     "location": {"name": "Remote"}, "updated_at": "2026-07-02T12:00:00-04:00"},
    {"id": 333, "title": "Marketing Intern", "absolute_url": "https://x/gh/333",
     "location": {"name": "Austin, TX"}, "updated_at": ""},
    {"id": 444, "title": "Security Engineer", "absolute_url": "https://x/gh/444",
     "location": {"name": "Remote"}, "updated_at": ""},
    {"id": 555, "title": "Associate Product Manager Intern", "absolute_url": "https://x/gh/555",
     "location": {"name": "NYC"}, "updated_at": ""},
]}

LV_DATA = [
    {"id": "abc", "text": "IT Intern", "hostedUrl": "https://x/lv/abc",
     "categories": {"location": "Fully Remote"}, "createdAt": 1780000000000},
    {"id": "def", "text": "Data Engineering Intern", "hostedUrl": "https://x/lv/def",
     "categories": {"location": "NYC"}, "createdAt": 1780000000000},
    {"id": "ghi", "text": "SOC Analyst Intern", "hostedUrl": "https://x/lv/ghi",
     "categories": {}, "createdAt": None},
]


# --- inclusion rule ----------------------------------------------------------
def test_relevant_intern_title_allowlist():
    assert ats_boards.relevant_intern_title("Security Engineer Intern")
    assert ats_boards.relevant_intern_title("Penetration Testing Internship")
    assert ats_boards.relevant_intern_title("IT Intern")
    assert ats_boards.relevant_intern_title("SOC Analyst Intern")
    assert not ats_boards.relevant_intern_title("Marketing Intern")
    assert not ats_boards.relevant_intern_title("Data Engineering Intern")
    # "soc" must not fire inside "Associate"
    assert not ats_boards.relevant_intern_title("Associate Product Manager Intern")


def test_relevant_intern_title_requires_internship():
    assert not ats_boards.relevant_intern_title("Security Engineer")
    # "internal" is not "intern"
    assert not ats_boards.relevant_intern_title("Internal IT Support Specialist")


# --- parsers -----------------------------------------------------------------
def test_parse_greenhouse_fixture():
    out = ats_boards._parse_greenhouse("Acme Sec", "acmesec", GH_DATA)
    assert [p["title"] for p in out] == ["Security Engineer Intern",
                                         "Penetration Testing Internship"]
    p = out[0]
    assert p["id"].startswith("ats:") and len(p["id"]) == 20
    assert p["company"] == "Acme Sec"
    assert p["link"] == "https://x/gh/111"
    assert p["location_str"] == "Boston, MA" and p["rank"] == 0
    assert p["source"] == "Greenhouse"
    assert p["deadline"] == "" and p["term"] == ""
    assert p["date_posted"] > 0
    assert out[1]["rank"] == 1  # Remote


def test_parse_lever_fixture():
    out = ats_boards._parse_lever("Acme Sec", "acmesec", LV_DATA)
    assert [p["title"] for p in out] == ["IT Intern", "SOC Analyst Intern"]
    assert out[0]["source"] == "Lever"
    assert out[0]["rank"] == 1  # Fully Remote
    assert out[0]["date_posted"] == 1780000000
    assert out[1]["location_str"] == "Unspecified" and out[1]["rank"] == 2
    assert out[1]["date_posted"] == 0


def test_ids_stable_across_parses():
    a = ats_boards._parse_greenhouse("Acme Sec", "acmesec", GH_DATA)
    b = ats_boards._parse_greenhouse("Acme Sec", "acmesec", GH_DATA)
    assert [p["id"] for p in a] == [p["id"] for p in b]


# --- companies.toml loader ---------------------------------------------------
def test_load_companies(tmp_path):
    p = tmp_path / "companies.toml"
    p.write_text('[[greenhouse]]\nname = "A"\nslug = "a"\n\n'
                 '[[lever]]\nname = "B"\nslug = "b"\n', encoding="utf-8")
    boards = ats_boards.load_companies(str(p))
    assert boards == [{"ats": "greenhouse", "name": "A", "slug": "a"},
                      {"ats": "lever", "name": "B", "slug": "b"}]


def test_load_companies_missing_file_is_empty(tmp_path):
    assert ats_boards.load_companies(str(tmp_path / "nope.toml")) == []


# --- fetch: per-board error isolation ---------------------------------------
class FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeSession:
    """Greenhouse board 'good' succeeds; anything else raises."""

    def get(self, url, **kwargs):
        if "good" in url:
            return FakeResp(GH_DATA)
        raise RuntimeError("board down")


def test_fetch_isolates_board_failure(tmp_path, monkeypatch):
    p = tmp_path / "companies.toml"
    p.write_text('[[greenhouse]]\nname = "Good Co"\nslug = "good"\n\n'
                 '[[greenhouse]]\nname = "Bad Co"\nslug = "bad"\n', encoding="utf-8")
    monkeypatch.setattr(ats_boards, "SESSION", FakeSession())
    out = ats_boards.fetch_ats_postings(str(p))
    # Bad Co failed but Good Co's two relevant postings survived
    assert len(out) == 2
    assert all(p["company"] == "Good Co" for p in out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_ats_boards.py -q`
Expected: collection ERROR — `ModuleNotFoundError: No module named 'ats_boards'`.

- [ ] **Step 3: Implement `ats_boards.py`**

Create `ats_boards.py`:

```python
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

from util import SESSION, USER_AGENT, location_rank, strip_html, word_match

logger = logging.getLogger("tracker.ats")

COMPANIES_PATH = "companies.toml"
GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"

# Security roles plus the classic entry-path roles into the field
# (the user's target: pen testing). Word-boundary matched — never substring.
CAREER_ALLOWLIST = (
    "security", "cyber", "pentest", "pen test", "penetration", "red team",
    "blue team", "appsec", "infosec", "soc analyst", "soc intern", "threat",
    "vulnerability", "incident", "forensic", "malware", "detection",
    "exploit", "identity", "iam", "grc", "network", "it intern",
    "information technology", "helpdesk", "help desk",
    "system administrator", "sysadmin", "technical support",
)


def relevant_intern_title(title):
    """Internship + security-career match. \\bintern(ship)?s?\\b avoids
    matching "internal"; the allowlist uses word_match so "soc" can't fire
    inside "Associate"."""
    low = title.lower()
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
        "link": link,
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
        title = strip_html(job.get("title", ""))
        if not relevant_intern_title(title):
            continue
        loc = (job.get("location") or {}).get("name", "")
        locations = [loc] if loc else []
        out.append(_posting("greenhouse", slug, name, job.get("id", ""), title,
                            job.get("absolute_url", ""), locations,
                            _to_epoch(job.get("updated_at"))))
    return out


def _parse_lever(name, slug, data):
    out = []
    for job in data if isinstance(data, list) else []:
        title = strip_html(job.get("text", ""))
        if not relevant_intern_title(title):
            continue
        loc = ((job.get("categories") or {}).get("location")) or ""
        locations = [loc] if loc else []
        created = job.get("createdAt")
        date_posted = int(created / 1000) if isinstance(created, (int, float)) else 0
        out.append(_posting("lever", slug, name, job.get("id", ""), title,
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error fetching %s board '%s': %s",
                           board["ats"], board["name"], exc)
            continue
        parse = _parse_greenhouse if board["ats"] == "greenhouse" else _parse_lever
        found = parse(board["name"], board["slug"], data)
        postings.extend(found)
        logger.info("%s (%s): %d relevant intern posting(s)",
                    board["name"], board["ats"], len(found))
    return postings
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q`
Expected: `52 passed` (44 + 8).

- [ ] **Step 5: Commit**

```bash
git add ats_boards.py tests/test_ats_boards.py
git commit -m "feat: ATS board source — Greenhouse/Lever intern postings, security-career allowlist

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Seed and verify `companies.toml`

**Files:**
- Create: `companies.toml`
- Create (throwaway, do NOT commit): a verify script run via stdin/heredoc

**Interfaces:**
- Consumes: `ats_boards.load_companies`, `ats_boards.GREENHOUSE_URL`/`LEVER_URL`, `util.SESSION`.
- Produces: committed `companies.toml` whose every slug resolved at build time (or, if the local sandbox blocks these hosts, the full candidate list committed with a DONE_WITH_CONCERNS report so the controller verifies via a deployed run).

- [ ] **Step 1: Write the candidate list**

Create `companies.toml`:

```toml
# Security-heavy companies whose ATS boards we poll daily.
# Adding a company is one line-pair: name + slug (the board identifier in
# the company's careers URL). Dead slugs are skipped with a logged warning.

[[greenhouse]]
name = "Cloudflare"
slug = "cloudflare"

[[greenhouse]]
name = "Datadog"
slug = "datadog"

[[greenhouse]]
name = "Elastic"
slug = "elastic"

[[greenhouse]]
name = "Okta"
slug = "okta"

[[greenhouse]]
name = "Snyk"
slug = "snyk"

[[greenhouse]]
name = "HackerOne"
slug = "hackerone"

[[greenhouse]]
name = "Trail of Bits"
slug = "trailofbits"

[[greenhouse]]
name = "SentinelOne"
slug = "sentinelone"

[[greenhouse]]
name = "Wiz"
slug = "wiz"

[[greenhouse]]
name = "Abnormal Security"
slug = "abnormalsecurity"

[[greenhouse]]
name = "Red Canary"
slug = "redcanary"

[[greenhouse]]
name = "Expel"
slug = "expel"

[[greenhouse]]
name = "Praetorian"
slug = "praetorian"

[[greenhouse]]
name = "Bishop Fox"
slug = "bishopfox"

[[greenhouse]]
name = "Synack"
slug = "synack"

[[greenhouse]]
name = "Huntress"
slug = "huntress"

[[greenhouse]]
name = "GitLab"
slug = "gitlab"

[[greenhouse]]
name = "Rubrik"
slug = "rubrik"

[[greenhouse]]
name = "Semgrep"
slug = "semgrep"

[[greenhouse]]
name = "Chainguard"
slug = "chainguard"

[[greenhouse]]
name = "Tailscale"
slug = "tailscale"

[[greenhouse]]
name = "1Password"
slug = "1password"

[[lever]]
name = "Netskope"
slug = "netskope"

[[lever]]
name = "Recorded Future"
slug = "recordedfuture"

[[lever]]
name = "Veracode"
slug = "veracode"

[[lever]]
name = "Arctic Wolf"
slug = "arcticwolf"

[[lever]]
name = "KnowBe4"
slug = "knowbe4"

[[lever]]
name = "Offensive Security"
slug = "offsec"
```

- [ ] **Step 2: Verify every slug resolves (one-time, network)**

Run from the repo root:

```bash
python3 - <<'EOF'
import ats_boards
from util import SESSION, USER_AGENT

ok, dead = [], []
for b in ats_boards.load_companies():
    url = (ats_boards.GREENHOUSE_URL if b["ats"] == "greenhouse"
           else ats_boards.LEVER_URL).format(slug=b["slug"])
    try:
        r = SESSION.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        n = len(r.json().get("jobs", []) if b["ats"] == "greenhouse" else r.json())
        ok.append((b["name"], b["ats"], n))
    except Exception as exc:
        dead.append((b["name"], b["ats"], b["slug"], str(exc)[:80]))
print("OK boards:")
for name, ats, n in ok: print(f"  {name} ({ats}): {n} total jobs")
print("DEAD boards (remove these):")
for row in dead: print(" ", row)
EOF
```

Expected: a list of OK boards with job counts and a (hopefully short) DEAD list.

- [ ] **Step 3: Prune dead slugs**

Remove every DEAD entry from `companies.toml`. If a big-name company is dead, try one alternate slug guess (e.g. company name without spaces) once; otherwise drop it — the file is user-editable later.

**If the network is unreachable from this sandbox** (connection errors for ALL boards): keep the full candidate list, do not prune, and report `DONE_WITH_CONCERNS` stating verification must happen via a deployed run — the daily run's per-board warnings identify dead slugs harmlessly.

- [ ] **Step 4: Run the full suite (unchanged)**

Run: `python3 -m pytest -q`
Expected: `52 passed` (companies.toml is data; no test change).

- [ ] **Step 5: Commit**

```bash
git add companies.toml
git commit -m "feat: seed companies.toml with verified security-company ATS boards

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Wire into `sources.py`, tune USAJOBS queries, README

**Files:**
- Modify: `sources.py` (import + `fetch_all_postings` + `USAJOBS_QUERIES`)
- Modify: `README.md` ("How it works" bullet 1)
- Test: `tests/test_tracker.py` (add 3 tests)

**Interfaces:**
- Consumes: `ats_boards.fetch_ats_postings()` (Task 2).
- Produces: `sources.fetch_all_postings()` including ATS postings; `sources.USAJOBS_QUERIES` with nationwide cybersecurity + information-security queries.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracker.py`:

```python
def test_usajobs_queries_tuned():
    assert ("cybersecurity intern", None) in sources.USAJOBS_QUERIES
    assert ("information security intern", None) in sources.USAJOBS_QUERIES
    assert ("cybersecurity intern", "Massachusetts") not in sources.USAJOBS_QUERIES
    assert len(sources.USAJOBS_QUERIES) <= 6


def test_fetch_all_postings_includes_ats_and_dedupes(monkeypatch):
    a = {"id": "x", "title": "A", "deadline": ""}
    dup = {"id": "x", "title": "A-dup", "deadline": ""}
    b = {"id": "y", "title": "B", "deadline": ""}
    monkeypatch.setattr(sources, "fetch_usajobs", lambda: [a])
    monkeypatch.setattr(sources, "fetch_github_lists", lambda: [dup])
    monkeypatch.setattr(sources.ats_boards, "fetch_ats_postings", lambda: [b])
    out = sources.fetch_all_postings()
    assert [p["id"] for p in out] == ["x", "y"]
    assert out[0]["title"] == "A"  # first source wins the dup


def test_fetch_all_postings_survives_ats_failure(monkeypatch):
    monkeypatch.setattr(sources, "fetch_usajobs", lambda: [])
    monkeypatch.setattr(sources, "fetch_github_lists",
                        lambda: [{"id": "y", "title": "B", "deadline": ""}])

    def boom():
        raise RuntimeError("ats exploded")

    monkeypatch.setattr(sources.ats_boards, "fetch_ats_postings", boom)
    out = sources.fetch_all_postings()
    assert [p["id"] for p in out] == ["y"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tracker.py -q`
Expected: 3 FAILs — MA-scoped query still present; `sources` has no attribute `ats_boards`.

- [ ] **Step 3: Implement**

In `sources.py`:

Add the import (after `from dateutil import parser as dateparser`):

```python
import ats_boards
```

Replace `USAJOBS_QUERIES` with:

```python
USAJOBS_QUERIES = [
    ("penetration tester intern", None),
    ("cybersecurity intern", None),
    ("information security intern", None),
    ("cybersecurity student trainee", None),
    ("information technology student trainee", "Massachusetts"),
]
```

Replace `fetch_all_postings` with:

```python
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
```

In `README.md`, replace the bullet:

```markdown
1. `sources.py` fetches postings from curated GitHub internship lists and
   **USAJOBS** (federal roles, which expose a real `ApplicationCloseDate`).
```

with:

```markdown
1. `sources.py` fetches postings from curated GitHub internship lists,
   **USAJOBS** (federal roles, which expose a real `ApplicationCloseDate`),
   and **security-company ATS boards** (Greenhouse/Lever) — edit
   `companies.toml` to add or remove companies.
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q`
Expected: `55 passed` (52 + 3).

- [ ] **Step 5: Commit**

```bash
git add sources.py README.md tests/test_tracker.py
git commit -m "feat: wire ATS boards into ingestion; nationwide USAJOBS queries

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: End-to-end offline smoke

**Files:** none (throwaway script; do not commit)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Run the smoke script**

```bash
python3 - <<'EOF'
from datetime import date
import ats_boards, scoring, sources

# ATS fixture -> full pipeline offline
gh = {"jobs": [{"id": 1, "title": "Penetration Testing Intern",
                "absolute_url": "https://x/1",
                "location": {"name": "Boston, MA"},
                "updated_at": "2026-07-01T00:00:00Z"}]}
posts = ats_boards._parse_greenhouse("SecCo", "secco", gh)
assert posts[0]["rank"] == 0 and posts[0]["id"].startswith("ats:")

import store as store_mod
store = {}
store_mod.merge_postings(store, posts, today="2026-07-14")
profile = {"weights": {"penetration test": 4}, "resume": "r", "version": "v"}
scoring.score_store(store, profile, gemini_fn=lambda *_: None,
                    today=date(2026, 7, 14))
rec = next(iter(store.values()))
assert rec["fit_score"] > 0, "suffix match through the full pipeline"
assert rec["rank_score"] > 0
print("ATS -> merge -> score OK:", rec["title"], rec["fit_score"], rec["rank_score"])
EOF
```

Expected output ends with `ATS -> merge -> score OK: Penetration Testing Intern <score> <rank>`.

- [ ] **Step 2: Full suite one last time**

Run: `python3 -m pytest -q`
Expected: `55 passed`

- [ ] **Step 3: Confirm branch state**

Run: `git status --short && git log --oneline -5`
Expected: clean tree; Task 1–4 commits on `feat/ats-board-coverage`.
