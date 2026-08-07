# Ranked, Skill-Matched Google Sheet — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the email digest with a live Google Sheet where every tracked posting is scored against the user's skillset and ranked so the top rows are "apply to these first."

**Architecture:** The `fetch → store` pipeline is unchanged. A new scoring stage (`profile.py`, `scoring.py`, `gemini_fit.py`) computes a per-posting fit score (deterministic keyword floor, overridden by Gemini when a key is present) and a blended rank. A new `sheet.py` reads user status edits back from the Sheet, then rewrites it ranked best-first. `digest.py` and all SMTP/email code are deleted; failure alerting is the GitHub issue the workflow already opens.

**Tech Stack:** Python 3.11 (stdlib `tomllib`), `gspread` + `google-auth` (Sheets), `google-genai` (Gemini), `requests`, `python-dateutil`, `pytest`.

## Global Constraints

- **Python 3.11** (workflow pins `3.11`; `tomllib` is stdlib there).
- **Dependency pins keep floors + block major bumps** (supply-chain hardening): `requests>=2.31.0,<3`, `python-dateutil>=2.8.2,<3`, `gspread>=6,<7`, `google-auth>=2.23,<3`, `google-genai>=1,<2`.
- **Tests are network-free** and pass with **no secrets set** (CI runs them without any Google/Gemini/USAJOBS credentials). All Gemini and Sheets I/O is dependency-injected and mocked in tests.
- **Graceful degradation is required:** no `GEMINI_API_KEY` → keyword floor; a Gemini failure on one posting → keyword fallback for that row, run continues. A single bad score never crashes a run.
- **`state/applications.json` stays the durable source of truth** (git-versioned). The Sheet is the interface; status is edited there and read back.
- **Blend weights:** `rank_score = 0.5·fit + 0.3·urgency + 0.2·location`.
- **Valid statuses** (unchanged): `new · interested · applied · interviewing · offer · rejected · skip`.

---

## File Structure

- Create `profile.toml` — the user's skillset: `[weights]`, `[boosts]`, `resume`.
- Create `profile.py` — load `profile.toml` into a dict + a content hash `version`.
- Create `scoring.py` — pure scoring (`keyword_fit`, `urgency_score`, `location_score`, `blend`, `posting_text`, `fit_hash`, `_days_until`) + `score_store` orchestration with caching and Gemini DI.
- Create `gemini_fit.py` — the isolated, mockable Gemini call + JSON parse.
- Create `sheet.py` — `build_rows`, `apply_status_edits`, `read_status_from_sheet`, `write_sheet` (worksheet dependency-injected).
- Modify `tracker.py` — rewire the pipeline; drop `digest`/email.
- Delete `digest.py`.
- Modify `tests/test_tracker.py` — drop digest/email tests; keep store/sources/util tests.
- Create `tests/test_profile.py`, `tests/test_scoring.py`, `tests/test_sheet.py`.
- Modify `requirements.txt`, `.github/workflows/track.yml`, `README.md`.

---

## Task 1: Profile file + loader

**Files:**
- Create: `profile.toml`
- Create: `profile.py`
- Test: `tests/test_profile.py`

**Interfaces:**
- Produces: `profile.load_profile(path="profile.toml") -> dict` with keys `weights: dict[str,int]`, `boosts: dict[str,bool]`, `resume: str`, `version: str` (12-char sha256 hex of the raw file bytes).

- [ ] **Step 1: Write `profile.toml`** (seeded from the user's résumé; user tunes later)

```toml
# Your skillset. Edit freely — higher weight = matters more to you.
# Changing this file re-scores every posting once (the cache keys on its hash).

[weights]                 # deterministic keyword floor
"web application" = 5
"web app" = 5
xss = 5
"sql injection" = 5
sqli = 5
"file upload" = 4
"api" = 4
burp = 4
proxy = 3
pentest = 4
"penetration test" = 4
nmap = 3
metasploit = 3
enumeration = 3
python = 3
fastapi = 3
bash = 2
linux = 2
ctf = 3
siem = 2
"incident response" = 2
"incident handling" = 2
"traffic analysis" = 2
"log analysis" = 2
"blue team" = 2
"red team" = 3

[boosts]
massachusetts = true

# The real résumé is supplied via the PROFILE_RESUME secret, not committed.
resume = """
Early-undergrad Computer Engineering student. Strongest in web-application
exploitation (web proxies, XSS, SQL injection, file upload, API attacks,
command injection; CTF web challenges). Pentest fundamentals (Nmap,
Metasploit, enumeration, methodology). Blue-team basics (SIEM fundamentals,
incident handling, network traffic and log analysis, Windows event logs).
Strong Python/FastAPI engineering. Also Java and Bash.
"""
```

- [ ] **Step 2: Write the failing test** — `tests/test_profile.py`

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import profile as profile_mod  # noqa: E402


def _write(tmp_path, text):
    p = tmp_path / "profile.toml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_load_profile_parses_sections(tmp_path):
    path = _write(
        tmp_path,
        '[weights]\n"web application" = 5\nxss = 5\n\n'
        '[boosts]\nmassachusetts = true\n\n'
        'resume = "I do web appsec."\n',
    )
    prof = profile_mod.load_profile(path)
    assert prof["weights"]["web application"] == 5
    assert prof["weights"]["xss"] == 5
    assert prof["boosts"]["massachusetts"] is True
    assert prof["resume"] == "I do web appsec."
    assert isinstance(prof["version"], str) and len(prof["version"]) == 12


def test_version_changes_when_file_changes(tmp_path):
    a = profile_mod.load_profile(_write(tmp_path, 'resume = "one"\n'))
    b = profile_mod.load_profile(_write(tmp_path, 'resume = "two"\n'))
    assert a["version"] != b["version"]


def test_missing_sections_default_empty(tmp_path):
    prof = profile_mod.load_profile(_write(tmp_path, 'resume = "x"\n'))
    assert prof["weights"] == {}
    assert prof["boosts"] == {}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_profile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'profile'` is shadowed; actually fails with `AttributeError`/import error because `profile.py` does not yet define `load_profile`.

- [ ] **Step 4: Write `profile.py`**

```python
"""
profile.py — load the user's skillset from profile.toml.

Two parts feed the two scorers: [weights] drives the deterministic keyword
floor; `resume` is handed to Gemini. `version` is a content hash so editing
the file re-scores every posting exactly once (it is part of the cache key).
"""

import hashlib
import tomllib

DEFAULT_PATH = "profile.toml"


def load_profile(path=DEFAULT_PATH):
    with open(path, "rb") as f:
        raw = f.read()
    data = tomllib.loads(raw.decode("utf-8"))
    return {
        "weights": {str(k).lower(): int(v) for k, v in data.get("weights", {}).items()},
        "boosts": dict(data.get("boosts", {})),
        "resume": str(data.get("resume", "")),
        "version": hashlib.sha256(raw).hexdigest()[:12],
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_profile.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add profile.toml profile.py tests/test_profile.py
git commit -m "feat: profile.toml + loader (weighted skillset for scoring)"
```

---

## Task 2: Pure scoring functions

**Files:**
- Create: `scoring.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `profile["weights"]` (`dict[str,int]`), posting records (dicts with `title`, `company`, `term`, `location_str`, `deadline`, `rank`).
- Produces:
  - `scoring.posting_text(rec: dict) -> str`
  - `scoring.keyword_fit(text: str, weights: dict) -> tuple[int, str]` — (0–100 score, reason string)
  - `scoring._days_until(deadline_iso: str, today: date | None = None) -> int | None`
  - `scoring.urgency_score(deadline_iso: str, today=None) -> float` — 0–100
  - `scoring.location_score(rank: int) -> float` — 0–100
  - `scoring.blend(fit: float, urgency: float, location: float) -> float`
  - `scoring.fit_hash(text: str, profile_version: str) -> str`
  - Constants: `FIT_WEIGHT=0.5`, `URGENCY_WEIGHT=0.3`, `LOCATION_WEIGHT=0.2`, `DEADLINE_WINDOW_DAYS=21`, `KEYWORD_FIT_TARGET=15.0`.

- [ ] **Step 1: Write the failing test** — `tests/test_scoring.py`

```python
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scoring  # noqa: E402

WEIGHTS = {"web application": 5, "xss": 5, "python": 3, "siem": 2}


def _rec(**kw):
    base = {"title": "", "company": "", "term": "", "location_str": "",
            "deadline": "", "rank": 2}
    base.update(kw)
    return base


# --- keyword_fit ------------------------------------------------------------
def test_keyword_fit_scores_and_reports_matches():
    text = "web application security intern — python"
    score, reason = scoring.keyword_fit(text, WEIGHTS)
    # matched 5 (web application) + 3 (python) = 8; 8/15*100 = 53
    assert score == 53
    assert "web application" in reason and "python" in reason


def test_keyword_fit_caps_at_100():
    text = "web application xss python siem"  # 5+5+3+2 = 15 -> 100
    score, _ = scoring.keyword_fit(text, WEIGHTS)
    assert score == 100


def test_keyword_fit_no_match():
    score, reason = scoring.keyword_fit("marketing manager", WEIGHTS)
    assert score == 0
    assert "no profile keywords" in reason.lower()


# --- urgency ----------------------------------------------------------------
def test_urgency_none_is_neutral_low():
    assert scoring.urgency_score("") == 20.0
    assert scoring.urgency_score(None) == 20.0


def test_urgency_closer_is_higher():
    today = date(2026, 1, 1)
    soon = (today + timedelta(days=0)).isoformat()
    later = (today + timedelta(days=21)).isoformat()
    assert scoring.urgency_score(soon, today) == 100.0
    assert scoring.urgency_score(later, today) == 40.0
    assert scoring.urgency_score(soon, today) > scoring.urgency_score(later, today)


def test_urgency_past_and_far():
    today = date(2026, 1, 1)
    assert scoring.urgency_score((today - timedelta(days=1)).isoformat(), today) == 0.0
    assert scoring.urgency_score((today + timedelta(days=60)).isoformat(), today) == 30.0


# --- location + blend -------------------------------------------------------
def test_location_score():
    assert scoring.location_score(0) == 100.0  # MA
    assert scoring.location_score(1) == 60.0   # remote
    assert scoring.location_score(2) == 20.0   # elsewhere


def test_blend_weights():
    # fit=100, urgency=0, location=0 -> 50 ; fit=0,urg=100,loc=0 -> 30
    assert scoring.blend(100, 0, 0) == 50.0
    assert scoring.blend(0, 100, 0) == 30.0
    assert scoring.blend(0, 0, 100) == 20.0


def test_fit_hash_changes_with_text_and_version():
    assert scoring.fit_hash("a", "v1") != scoring.fit_hash("b", "v1")
    assert scoring.fit_hash("a", "v1") != scoring.fit_hash("a", "v2")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scoring.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scoring'`

- [ ] **Step 3: Write `scoring.py`** (pure functions only; `score_store` added in Task 4)

```python
"""
scoring.py — score each posting against the user's profile and blend into a rank.

fit    : how well the role matches your skillset (keyword floor; Gemini overrides
         it when a key is present — see score_store in this module, Task 4).
urgency: decays from the application deadline.
location: MA/remote/elsewhere boost.

rank_score = 0.5*fit + 0.3*urgency + 0.2*location  (all on a 0-100 scale)
"""

import hashlib
import logging
from datetime import date, datetime

logger = logging.getLogger("tracker.scoring")

FIT_WEIGHT = 0.5
URGENCY_WEIGHT = 0.3
LOCATION_WEIGHT = 0.2
DEADLINE_WINDOW_DAYS = 21
# Matched keyword-weight that counts as a top (100) fit. With weights of 3-5,
# hitting ~3-4 strong terms saturates the score.
KEYWORD_FIT_TARGET = 15.0


def posting_text(rec):
    """The text we have to match against (titles + USAJOBS's richer fields)."""
    parts = [rec.get("title", ""), rec.get("company", ""),
             str(rec.get("term", "")), rec.get("location_str", "")]
    return " ".join(p for p in parts if p)


def keyword_fit(text, weights):
    """Deterministic 0-100 fit from weighted keyword overlap + a reason string."""
    low = text.lower()
    matched = [(kw, w) for kw, w in weights.items() if kw in low]
    if not matched:
        return 0, "no profile keywords matched"
    total = sum(w for _, w in matched)
    score = int(min(100.0, total / KEYWORD_FIT_TARGET * 100.0))
    terms = ", ".join(kw for kw, _ in sorted(matched, key=lambda t: -t[1]))
    return score, f"matched: {terms}"


def _days_until(deadline_iso, today=None):
    if not deadline_iso:
        return None
    try:
        d = datetime.strptime(deadline_iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (d - (today or date.today())).days


def urgency_score(deadline_iso, today=None):
    """0-100. No deadline -> neutral-low (20). 0 days -> 100, window -> 40."""
    days = _days_until(deadline_iso, today)
    if days is None:
        return 20.0
    if days < 0:
        return 0.0
    if days > DEADLINE_WINDOW_DAYS:
        return 30.0
    return 100.0 - (days / DEADLINE_WINDOW_DAYS) * 60.0


def location_score(rank):
    """Invert the MA=0 / remote=1 / else=2 rank into a 0-100 boost."""
    return {0: 100.0, 1: 60.0}.get(rank, 20.0)


def blend(fit, urgency, location):
    return round(FIT_WEIGHT * fit + URGENCY_WEIGHT * urgency + LOCATION_WEIGHT * location, 2)


def fit_hash(text, profile_version):
    return hashlib.sha256(f"{profile_version}\x00{text}".encode("utf-8")).hexdigest()[:16]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_scoring.py -v`
Expected: PASS (all scoring tests pass)

- [ ] **Step 5: Commit**

```bash
git add scoring.py tests/test_scoring.py
git commit -m "feat: pure scoring functions (keyword fit, urgency, location, blend)"
```

---

## Task 3: Isolated Gemini fit client

**Files:**
- Create: `gemini_fit.py`
- Test: `tests/test_scoring.py` (append)

**Interfaces:**
- Produces:
  - `gemini_fit.gemini_fit(resume: str, posting_text: str) -> tuple[int, str] | None` — returns `(score 0-100, reason)`, or `None` when `GEMINI_API_KEY` is unset. Raises on API/parse failure (caller catches).
  - `gemini_fit._parse_fit(text: str) -> tuple[int, str]` — pure parser used in tests.
  - Constant `GEMINI_MODEL` (default `"gemini-flash-lite-latest"`; change to match your account).

- [ ] **Step 1: Write the failing test** — append to `tests/test_scoring.py`

```python
import gemini_fit  # noqa: E402


def test_gemini_parse_fit_clamps_and_extracts():
    score, reason = gemini_fit._parse_fit('{"score": 130, "reason": "strong web"}')
    assert score == 100
    assert reason == "strong web"
    score2, _ = gemini_fit._parse_fit('```json\n{"score": -5, "reason": "weak"}\n```')
    assert score2 == 0


def test_gemini_fit_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert gemini_fit.gemini_fit("resume", "posting") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scoring.py -k gemini -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gemini_fit'`

- [ ] **Step 3: Write `gemini_fit.py`** (mirrors [personal project]'s structured-output pattern)

```python
"""
gemini_fit.py — isolated Gemini call that scores one posting against the résumé.

Kept in its own module so the only network dependency in scoring lives in one
place and is easy to mock (score_store injects it). Returns None when no key is
set so the caller falls back to the deterministic keyword score.
"""

import json
import logging
import os

logger = logging.getLogger("tracker.gemini")

GEMINI_MODEL = "gemini-flash-lite-latest"  # adjust to your account's model id

_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["score", "reason"],
}


def _prompt(resume, posting_text):
    return (
        "You rank cybersecurity internships for a specific candidate. Given the "
        "candidate profile and a job posting, return ONLY a JSON object "
        '{"score": int 0-100, "reason": string}. score = how well THIS posting '
        "matches THIS candidate's skills and level (higher = apply sooner). Use "
        "the full 0-100 range; do not cluster on multiples of 5. reason = ONE "
        "short sentence citing concrete overlap or gaps (e.g. 'strong: web "
        "exploitation + Python; weak: senior-level, cloud-heavy'). Do not wrap "
        "the JSON in markdown.\n\n"
        f"CANDIDATE PROFILE:\n{resume.strip()}\n\n"
        f"JOB POSTING:\n{posting_text.strip()}"
    )


def _parse_fit(text):
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[len("json"):]
        cleaned = cleaned.strip().rstrip("`").strip()
    data = json.loads(cleaned)
    score = max(0, min(100, int(data["score"])))
    return score, str(data.get("reason", ""))


def gemini_fit(resume, posting_text):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[_prompt(resume, posting_text)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_SCHEMA,
        ),
    )
    return _parse_fit(response.text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_scoring.py -k gemini -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add gemini_fit.py tests/test_scoring.py
git commit -m "feat: isolated Gemini fit client (structured JSON, key-gated)"
```

---

## Task 4: score_store orchestration (caching + Gemini override + fallback)

**Files:**
- Modify: `scoring.py`
- Test: `tests/test_scoring.py` (append)

**Interfaces:**
- Consumes: `store` (dict of records), `profile` (from `load_profile`), `gemini_fn` (defaults to `gemini_fit.gemini_fit`), `active_records` from `store.py`.
- Produces: `scoring.score_store(store, profile, gemini_fn=<gemini_fit>, today=None) -> None` — mutates each active record in place, setting `fit_score:int`, `fit_reason:str`, `fit_hash:str`, `rank_score:int`. Skips re-scoring when `fit_hash` is unchanged (but always recomputes `rank_score`, since urgency changes daily).

- [ ] **Step 1: Write the failing test** — append to `tests/test_scoring.py`

```python
import store as store_mod  # noqa: E402

PROFILE = {"weights": WEIGHTS, "boosts": {}, "resume": "web appsec + python",
           "version": "v1"}


def _seed(**kw):
    store = {}
    rec = _rec(**kw)
    rec.update({"id": kw.get("id", "a"), "status": "new"})
    store[rec["id"]] = rec
    return store


def test_score_store_keyword_when_no_gemini():
    store = _seed(title="Web Application Security Intern", rank=0,
                  deadline="")
    scoring.score_store(store, PROFILE, gemini_fn=lambda *_: None, today=date(2026, 1, 1))
    rec = store["a"]
    assert rec["fit_score"] == 33   # only "web application" (5) -> 5/15*100
    assert "web application" in rec["fit_reason"]
    assert rec["rank_score"] > 0


def test_score_store_gemini_overrides_keyword():
    store = _seed(title="Generic Security Intern")
    scoring.score_store(store, PROFILE, gemini_fn=lambda *_: (77, "strong web"),
                        today=date(2026, 1, 1))
    assert store["a"]["fit_score"] == 77
    assert store["a"]["fit_reason"] == "strong web"


def test_score_store_falls_back_when_gemini_raises():
    store = _seed(title="Web Application Security Intern")

    def boom(*_):
        raise RuntimeError("gemini down")

    scoring.score_store(store, PROFILE, gemini_fn=boom, today=date(2026, 1, 1))
    assert store["a"]["fit_score"] == 33  # keyword fallback, not a crash


def test_score_store_caches_unchanged(monkeypatch):
    store = _seed(title="Web Application Security Intern")
    calls = {"n": 0}

    def counting(*_):
        calls["n"] += 1
        return (80, "match")

    scoring.score_store(store, PROFILE, gemini_fn=counting, today=date(2026, 1, 1))
    scoring.score_store(store, PROFILE, gemini_fn=counting, today=date(2026, 1, 2))
    assert calls["n"] == 1  # second run reused the cached fit


def test_score_store_reprices_when_profile_version_changes():
    store = _seed(title="Web Application Security Intern")
    scoring.score_store(store, PROFILE, gemini_fn=lambda *_: (10, "x"),
                        today=date(2026, 1, 1))
    bumped = dict(PROFILE, version="v2")
    scoring.score_store(store, bumped, gemini_fn=lambda *_: (90, "y"),
                        today=date(2026, 1, 1))
    assert store["a"]["fit_score"] == 90  # re-scored because version changed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scoring.py -k score_store -v`
Expected: FAIL — `AttributeError: module 'scoring' has no attribute 'score_store'`

- [ ] **Step 3: Add `score_store` to `scoring.py`** (append; add the import at the top)

At the top of `scoring.py`, add below the existing imports:

```python
import gemini_fit as _gemini
from store import active_records
```

At the end of `scoring.py`, add:

```python
def score_store(store, profile, gemini_fn=_gemini.gemini_fit, today=None):
    """Score every active record in place. Cache by (posting text + profile
    version); always recompute rank_score since urgency changes daily."""
    weights = profile.get("weights", {})
    resume = profile.get("resume", "")
    version = profile.get("version", "")
    for rec in active_records(store):
        text = posting_text(rec)
        h = fit_hash(text, version)
        if rec.get("fit_hash") == h and "fit_score" in rec:
            fit = rec["fit_score"]  # reuse cached fit
        else:
            kw_score, kw_reason = keyword_fit(text, weights)
            result = None
            if gemini_fn is not None:
                try:
                    result = gemini_fn(resume, text)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Gemini fit failed for %s: %s", rec.get("id"), exc)
            if result:
                fit, reason = int(result[0]), str(result[1])
            else:
                fit, reason = kw_score, kw_reason
            rec["fit_score"] = fit
            rec["fit_reason"] = reason
            rec["fit_hash"] = h
        urgency = urgency_score(rec.get("deadline"), today)
        location = location_score(rec.get("rank", 2))
        rec["rank_score"] = round(blend(rec["fit_score"], urgency, location))
```

- [ ] **Step 4: Run the full scoring suite to verify it passes**

Run: `python -m pytest tests/test_scoring.py -v`
Expected: PASS (all scoring + gemini + score_store tests pass)

- [ ] **Step 5: Commit**

```bash
git add scoring.py tests/test_scoring.py
git commit -m "feat: score_store — Gemini override, keyword fallback, per-posting cache"
```

---

## Task 5: Google Sheet writer + status read-back

**Files:**
- Create: `sheet.py`
- Test: `tests/test_sheet.py`

**Interfaces:**
- Consumes: `store` records (with `rank_score`, `fit_score`, `fit_reason`, etc.), `store.active_records`, `store.VALID_STATUSES`, `scoring._days_until`.
- Produces:
  - `sheet.COLUMNS: list[str]` (ends with `"ID"`).
  - `sheet.build_rows(store) -> list[list]` — header row + one row per active record, sorted by `rank_score` desc.
  - `sheet.apply_status_edits(store, status_by_id: dict) -> None` — fold valid status edits into the store.
  - `sheet.read_status_from_sheet(worksheet) -> dict[str,str]` — map posting id → status from an existing sheet.
  - `sheet.write_sheet(store, worksheet=None) -> None` — clear + write ranked rows (opens the sheet if `worksheet` is None).

- [ ] **Step 1: Write the failing test** — `tests/test_sheet.py`

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sheet  # noqa: E402
import store as store_mod  # noqa: E402


class FakeWS:
    """Stands in for a gspread worksheet (no network)."""

    def __init__(self, values=None):
        self._values = values or []
        self.cleared = False
        self.updated = None

    def get_all_values(self):
        return self._values

    def clear(self):
        self.cleared = True

    def update(self, range_name=None, values=None, **kwargs):
        self.updated = (range_name, values)


def _rec(pid, rank_score, status="new", title="Intern"):
    return {"id": pid, "title": title, "company": "Acme", "location_str": "Remote",
            "term": "Summer 2027", "source": "test", "link": f"https://x/{pid}",
            "deadline": "", "first_seen": "2026-01-01", "status": status,
            "rank_score": rank_score, "fit_score": 50, "fit_reason": "matched: python"}


def test_build_rows_header_and_ranking():
    store = {"a": _rec("a", 40), "b": _rec("b", 90)}
    rows = sheet.build_rows(store)
    assert rows[0] == sheet.COLUMNS
    assert rows[0][-1] == "ID"
    # highest rank_score first
    id_col = sheet.COLUMNS.index("ID")
    assert rows[1][id_col] == "b"
    assert rows[2][id_col] == "a"


def test_build_rows_excludes_rejected():
    store = {"a": _rec("a", 40, status="rejected"), "b": _rec("b", 90)}
    rows = sheet.build_rows(store)
    assert len(rows) == 2  # header + one active row


def test_apply_status_edits_valid_and_invalid():
    store = {"a": _rec("a", 40)}
    sheet.apply_status_edits(store, {"a": "applied", "ghost": "applied",
                                     "a2": "bogus"})
    assert store["a"]["status"] == "applied"  # valid edit applied
    assert "ghost" not in store            # unknown id ignored


def test_read_status_from_sheet():
    header = sheet.COLUMNS
    row = [""] * len(header)
    row[header.index("ID")] = "a"
    row[header.index("Status")] = "Applied"
    ws = FakeWS([header, row])
    assert sheet.read_status_from_sheet(ws) == {"a": "applied"}


def test_write_sheet_clears_and_updates():
    store = {"b": _rec("b", 90)}
    ws = FakeWS()
    sheet.write_sheet(store, worksheet=ws)
    assert ws.cleared is True
    assert ws.updated is not None
    range_name, values = ws.updated
    assert values[0] == sheet.COLUMNS
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_sheet.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sheet'`

- [ ] **Step 3: Write `sheet.py`**

```python
"""
sheet.py — the ranked Google Sheet (replaces the old email digest).

Reads the user's Status edits back from the sheet into the store, then rewrites
one worksheet, one row per active posting, sorted best-first by rank_score.
Status is the interface; state/applications.json stays the durable store.
"""

import json
import logging
import os

from scoring import _days_until
from store import VALID_STATUSES, active_records

logger = logging.getLogger("tracker.sheet")

COLUMNS = ["#", "Score", "Fit", "Why", "Role", "Company", "Location",
           "Deadline", "Days left", "Status", "Source", "Link", "First seen", "ID"]


def build_rows(store):
    recs = sorted(active_records(store), key=lambda r: r.get("rank_score", 0),
                  reverse=True)
    rows = [list(COLUMNS)]
    for i, r in enumerate(recs, 1):
        days = _days_until(r.get("deadline"))
        rows.append([
            i,
            r.get("rank_score", 0),
            r.get("fit_score", 0),
            r.get("fit_reason", ""),
            r.get("title", ""),
            r.get("company", ""),
            r.get("location_str", ""),
            r.get("deadline", ""),
            "" if days is None else days,
            r.get("status", "new"),
            r.get("source", ""),
            r.get("link", ""),
            r.get("first_seen", ""),
            r.get("id", ""),
        ])
    return rows


def apply_status_edits(store, status_by_id):
    for pid, status in status_by_id.items():
        if pid in store and status in VALID_STATUSES:
            store[pid]["status"] = status


def read_status_from_sheet(worksheet=None):
    ws = worksheet or _open_worksheet()
    values = ws.get_all_values()
    if not values:
        return {}
    header = values[0]
    try:
        id_i = header.index("ID")
        st_i = header.index("Status")
    except ValueError:
        return {}
    out = {}
    for row in values[1:]:
        if len(row) > max(id_i, st_i):
            pid = row[id_i].strip()
            status = row[st_i].strip().lower()
            if pid:
                out[pid] = status
    return out


def write_sheet(store, worksheet=None):
    ws = worksheet or _open_worksheet()
    rows = build_rows(store)
    ws.clear()
    ws.update(range_name="A1", values=rows)
    logger.info("Wrote %d posting(s) to the sheet.", len(rows) - 1)


def _open_worksheet():
    import gspread

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("GSHEET_ID")
    missing = [n for n, v in (("GOOGLE_SERVICE_ACCOUNT_JSON", raw),
                              ("GSHEET_ID", sheet_id)) if not v]
    if missing:
        raise RuntimeError(f"Missing sheet env var(s): {', '.join(missing)}")
    gc = gspread.service_account_from_dict(json.loads(raw))
    return gc.open_by_key(sheet_id).sheet1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_sheet.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add sheet.py tests/test_sheet.py
git commit -m "feat: ranked Google Sheet writer + status read-back"
```

---

## Task 6: Rewire the entry point; delete the email digest

**Files:**
- Modify: `tracker.py`
- Delete: `digest.py`
- Modify: `tests/test_tracker.py`

**Interfaces:**
- Consumes: `sources.fetch_all_postings`, `store.load_store/merge_postings/save_store`, `profile.load_profile`, `scoring.score_store`, `sheet.read_status_from_sheet/apply_status_edits/write_sheet/_open_worksheet`.
- Produces: `tracker.main()` — the full daily pipeline. On any exception it logs and exits non-zero (the workflow opens the failure issue).

- [ ] **Step 1: Delete `digest.py` and prune its tests**

```bash
git rm digest.py
```

Edit `tests/test_tracker.py`: remove the `import digest` line and delete the two digest tests (`test_days_until`, `test_digest_surfaces_closing_soon`, `test_applied_not_nagged_for_deadline`) and the `from datetime import date, timedelta` import if now unused. The file should keep only the store, `location_rank`, and sanitization tests. Final `tests/test_tracker.py`:

```python
"""Network-free unit tests for the tracker's store, ranking inputs, and sanitizers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sources  # noqa: E402
import store as store_mod  # noqa: E402
import util  # noqa: E402


def _posting(pid, title="Pentest Intern", deadline="", rank=2):
    return {
        "id": pid,
        "title": title,
        "company": "Acme",
        "link": f"https://example.com/{pid}",
        "locations": ["Boston, MA"] if rank == 0 else ["Remote"],
        "location_str": "Boston, MA" if rank == 0 else "Remote",
        "source": "test",
        "term": "Summer 2027",
        "date_posted": 0,
        "deadline": deadline,
        "rank": rank,
    }


# --- store ------------------------------------------------------------------
def test_first_run_is_baseline():
    store = {}
    new = store_mod.merge_postings(store, [_posting("a"), _posting("b")], today="2026-07-08")
    assert new == []
    assert len(store) == 2


def test_second_run_flags_new_and_preserves_status():
    store = {}
    store_mod.merge_postings(store, [_posting("a")], today="2026-07-07")
    store["a"]["status"] = "applied"
    store["a"]["notes"] = "referred by X"
    new = store_mod.merge_postings(store, [_posting("a"), _posting("b")], today="2026-07-08")
    assert new == ["b"]
    assert store["a"]["status"] == "applied"
    assert store["a"]["notes"] == "referred by X"


def test_volatile_fields_refresh():
    store = {}
    store_mod.merge_postings(store, [_posting("a", deadline="")], today="2026-07-07")
    store_mod.merge_postings(store, [_posting("a", deadline="2026-08-01")], today="2026-07-08")
    assert store["a"]["deadline"] == "2026-08-01"


# --- ranking inputs + sanitization -----------------------------------------
def test_location_rank():
    assert sources.location_rank(["Boston, MA"]) == 0
    assert sources.location_rank(["Remote"]) == 1
    assert sources.location_rank(["Austin, TX"]) == 2


def test_safe_url_and_md_escape():
    assert util.safe_url("javascript:alert(1)") == ""
    assert util.safe_url("https://ok.com") == "https://ok.com"
    assert "\\[" in util.md_escape("a [b](c)")
    assert "<img" not in util.strip_html("x <img onerror=1> y")
```

- [ ] **Step 2: Run the suite to confirm it is green without digest**

Run: `python -m pytest -q`
Expected: PASS — no `ModuleNotFoundError: digest`; all remaining tests pass.

- [ ] **Step 3: Rewrite `tracker.py`**

```python
"""
tracker.py — entry point. Read status edits back from the Sheet, fetch postings,
fold them into the store, score them against your profile, and rewrite the
ranked Google Sheet. Run daily by GitHub Actions. On failure the workflow opens
a GitHub issue (no email).
"""

import logging
import sys
import traceback

import scoring
import sheet
import sources
from profile import load_profile
from store import load_store, merge_postings, save_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tracker")


def main():
    prof = load_profile()
    worksheet = sheet._open_worksheet()

    store = load_store()
    # 1) fold the user's in-sheet Status edits back into the durable store.
    sheet.apply_status_edits(store, sheet.read_status_from_sheet(worksheet))

    # 2) ingest + merge.
    postings = sources.fetch_all_postings()
    new_ids = merge_postings(store, postings)

    # 3) score + rank.
    scoring.score_store(store, prof)
    save_store(store)

    # 4) rewrite the ranked sheet.
    sheet.write_sheet(store, worksheet)
    logger.info("Done — %d tracked, %d new.", len(store), len(new_ids))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        logger.critical("Tracker run failed: %s\n%s", exc, traceback.format_exc())
        sys.exit(1)
```

- [ ] **Step 4: Verify `tracker.py` imports cleanly (no network)**

Run: `python -c "import tracker; print('ok')"`
Expected: prints `ok` (imports resolve; `main()` not called).

- [ ] **Step 5: Commit**

```bash
git add tracker.py tests/test_tracker.py
git commit -m "refactor: rewire tracker to score + write sheet; drop email digest"
```

---

## Task 7: Dependencies, workflow, and README

**Files:**
- Modify: `requirements.txt`
- Modify: `.github/workflows/track.yml`
- Modify: `README.md`

**Interfaces:** none (config + docs).

- [ ] **Step 1: Rewrite `requirements.txt`**

```
# Version floors keep known-good behavior; upper bounds block surprise major
# bumps (supply-chain hardening). Dependabot proposes reviewed updates.
requests>=2.31.0,<3
python-dateutil>=2.8.2,<3
gspread>=6,<7
google-auth>=2.23,<3
google-genai>=1,<2
```

- [ ] **Step 2: Install and run the full suite**

Run: `pip install -r requirements.txt pytest && python -m pytest -q`
Expected: PASS (all tests across profile/scoring/sheet/tracker suites).

- [ ] **Step 3: Update the workflow env block** — `.github/workflows/track.yml`

Replace the `Run tracker` step's `env:` block (the `EMAIL_*` and `USAJOBS_*` vars) with:

```yaml
      - name: Run tracker
        env:
          GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
          GSHEET_ID: ${{ secrets.GSHEET_ID }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          # Optional — enables federal postings (with real deadlines) via USAJOBS.
          USAJOBS_API_KEY: ${{ secrets.USAJOBS_API_KEY }}
          USAJOBS_EMAIL: ${{ secrets.USAJOBS_EMAIL }}
        run: python tracker.py
```

Leave the `checkout`, `setup-python`, `Install dependencies`, `Persist applications store`, and `Open alert issue on failure` steps unchanged.

- [ ] **Step 4: Rewrite `README.md`** to describe the sheet model

Replace the file with:

```markdown
# 🎯 Internship Tracker

Tracks cybersecurity internships over time and keeps a **live Google Sheet**,
ranked so the top rows are the ones to **apply to first** — weighted toward how
well each role matches *your* skillset, how soon it closes, and whether it's in
Massachusetts / remote.

Runs daily via GitHub Actions.

## How it works

1. `sources.py` fetches postings from curated GitHub internship lists and
   **USAJOBS** (federal roles, which expose a real `ApplicationCloseDate`).
2. `store.py` folds them into a persistent store (`state/applications.json`),
   remembering your **status** and **notes** across runs.
3. `scoring.py` scores each posting against your `profile.toml`:
   - a deterministic **keyword** floor (always), and
   - a **Gemini** fit score + one-line rationale when `GEMINI_API_KEY` is set
     (it overrides the keyword score and adds the "Why" column).
   Then it blends `rank = 0.5·fit + 0.3·urgency + 0.2·location`.
4. `sheet.py` rewrites the Google Sheet, best-first.

## Your profile

Edit **`profile.toml`** — weighted keywords (`[weights]`) drive the deterministic
score; the `resume` blob is what Gemini matches against. Higher weight = matters
more. Editing the file re-scores every posting once.

## Status

Set status **in the Sheet** (the `Status` column). Each run reads your edits back
into `state/applications.json` before rewriting. Statuses:
`new · interested · applied · interviewing · offer · rejected · skip`
(`rejected`/`skip` drop off the sheet).

## Setup

In a GitHub repo, add these **Secrets** (Settings → Secrets and variables → Actions):

| Secret | Value |
|--------|-------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | full JSON key for a Google service account |
| `GSHEET_ID` | the target spreadsheet's ID (from its URL) |
| `GEMINI_API_KEY` *(optional)* | enables Gemini fit scoring + rationale |
| `USAJOBS_API_KEY` *(optional)* | free key from developer.usajobs.gov — deadline-bearing federal postings |
| `USAJOBS_EMAIL` *(optional)* | the email you registered with USAJOBS |

**Google Sheet setup:** create a Google Cloud service account, enable the Google
Sheets API, download its JSON key (→ `GOOGLE_SERVICE_ACCOUNT_JSON`), create a
spreadsheet, and **share it with the service account's email** (Editor). Put the
spreadsheet's ID in `GSHEET_ID`.

## Development

```bash
pip install -r requirements.txt pytest
pytest                 # network-free; needs no secrets
python sources.py      # inspect fetched postings
```

## Roadmap

- **Phase 1:** ingest + persistent store + status ✅
- **Phase 2 (this):** skill-matched ranking + live Google Sheet ✅
- **Phase 3:** ATS-page deadline scraping (Greenhouse/Lever/Workday) for richer deadline coverage
```

- [ ] **Step 5: Commit**

```bash
git add requirements.txt .github/workflows/track.yml README.md
git commit -m "chore: deps (gspread/google-genai), workflow env, README for sheet model"
```

---

## Self-Review Notes

- **Spec coverage:** output=Google Sheet (Task 5); blend 0.5/0.3/0.2 (Task 2); Gemini override + keyword floor + fallback (Tasks 3–4); caching by posting+profile hash (Task 4); `profile.toml` via tomllib (Task 1); status read-back (Tasks 5–6); email removed / failure→issue (Tasks 6–7); deps + secrets swap (Task 7); network-free tests (all). Correction vs spec: dependency is **`google-genai`** (not `google-generativeai`) — matches [personal project]'s `from google import genai`.
- **Deferred (as designed):** ATS scraping, multi-tab layout, conditional-formatting/data-validation are not implemented — `write_sheet` writes values only. The Status dropdown is a manual one-time setup on the sheet (or a future enhancement); read-back works regardless of whether the cell is a free-text or dropdown entry. Noted so no task silently drops a spec requirement.
- **Type consistency:** `score_store` writes `fit_score/fit_reason/fit_hash/rank_score`; `build_rows` reads exactly those. `read_status_from_sheet`/`apply_status_edits` key on the `ID` column that `build_rows` writes last.
```
