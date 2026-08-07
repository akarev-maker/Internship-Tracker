# Design: Ranked, Skill-Matched Google Sheet

**Date:** 2026-07-08
**Status:** Approved (brainstorming) — pending implementation plan

## Summary

Pivot the Internship Tracker's output from a daily **email digest** to a **live
Google Sheet** where every tracked posting is **scored against the user's
skillset and ranked** so the top rows are "apply to these first."

The ingest/store pipeline (`sources.py`, `store.py`) is unchanged. The final
stage changes: `digest.py` (email) is **replaced** by a scoring engine plus a
Google Sheets writer. All SMTP/email code is removed; failure alerting moves to
the GitHub issue the workflow already opens on failure.

## Motivation

The user does not want email. They want one ranked spreadsheet that tells them
where to apply first, weighted toward how well each role matches their skillset.

## User skill profile (source material for matching)

The real profile is personal and lives outside this repo — the `[weights]` in
`profile.toml` plus the `PROFILE_RESUME` secret. The shape it takes:

- Early undergrad, B.S. Computer Engineering, with a known graduation year.
- **Strongest lane: web-application exploitation** — web proxies, XSS, SQLi,
  file upload, API attacks, command injection; CTF web challenges.
- **Pentest fundamentals:** Nmap, Metasploit, footprinting, methodology.
- **Blue-team basics:** SIEM fundamentals, incident handling, traffic/log
  analysis, Windows event logs.
- **Engineering:** strong Python/FastAPI, Java, Bash.
- **Location signal:** a home state that is doubly relevant (school + jobs).

## Decisions (from brainstorming)

1. **Output form:** a live **Google Sheet**, updated by the daily run. Native
   sort/filter/annotate. Requires a service-account key + a shared sheet.
2. **Ranking:** a **blend** — `rank_score = 0.5·fit + 0.3·urgency + 0.2·location`.
   Fit-led, with a near deadline or a MA/remote role boosting the row.
3. **Fit scoring:** **Gemini when a key is present, deterministic keyword floor
   always.** Gemini *overrides* (not averages with) the keyword score and adds a
   one-line rationale. Keyword floor is the value when no key is set.
4. **Status:** edited **in the Sheet** (dropdown), read back into
   `applications.json` at the start of each run. JSON stays the durable,
   git-versioned source of truth; the Sheet is the interface on top of it.
5. **Layout:** one flat worksheet (no per-section tabs). Sorting/filtering in
   Sheets replaces the old digest sections.
6. **No ATS scraping yet** — fit sees only the text we already have (good titles
   + USAJOBS's richer text). Deliberately deferred (too brittle).

## Architecture

```
tracker.py ── entry point (daily CI)
   ├─▶ sources.py    (unchanged) fetch + normalize postings
   ├─▶ profile.toml  NEW — skillset: weighted keywords + a résumé blob
   ├─▶ scoring.py    NEW — fit (keyword floor + Gemini when key present) → blended rank
   ├─▶ store.py      + caches fit score/rationale; only re-scores changed postings
   └─▶ sheet.py      REPLACES digest.py — reads status back, writes the ranked sheet
```

### Data flow per run

1. `sheet.py` reads the Sheet's Status column (keyed by posting `id`) and folds
   user edits back into `applications.json`.
2. `sources.fetch_all_postings()` — existing sources.
3. `store.merge_postings()` — merge into `state/applications.json`.
4. `scoring.score_store()` — for each active posting: keyword fit always; Gemini
   fit + rationale when `GEMINI_API_KEY` set; cache by
   `fit_hash = hash(posting_text + profile_version)` to skip unchanged rows.
5. Compute `rank_score` blend.
6. `sheet.write_sheet()` — rewrite the worksheet sorted by `rank_score` desc.

## Components

### `profile.toml` (read with stdlib `tomllib`, no new dep)

```toml
[weights]                 # deterministic floor; higher weight = matters more
"web application" = 5
xss = 5
"sql injection" = 5
burp = 4
pentest = 4
nmap = 3
metasploit = 3
python = 3
siem = 2
"incident response" = 2
# seeded from the résumé; user tunes over time

[boosts]
massachusetts = true

# Illustrative only — the real text comes from the PROFILE_RESUME secret.
resume = """
Early-undergrad CE student. Strongest in web-application exploitation
(proxies, XSS, SQLi, file upload, API attacks; CTF web challenges). Pentest
fundamentals (Nmap, Metasploit). Blue-team basics (SIEM, incident handling,
traffic/log analysis). Strong Python/FastAPI.
"""
```

A `profile_version` — a short hash of the file's contents (`[weights]` +
`[boosts]` + `resume`) — feeds the cache key, so editing the profile re-scores
everything exactly once.

### `scoring.py`

Produces per posting: `fit_score` (0–100), `fit_reason` (str), `rank_score`.

- **Keyword floor (always):** sum weights of profile keywords found in the
  posting text; normalize to 0–100. `fit_reason` = matched terms. Deterministic,
  unit-testable, and the value when no API key is present.
- **Gemini layer (when `GEMINI_API_KEY` set):** send `{resume blob + posting
  text}` → structured JSON `{score 0-100, reason}`. **Overrides** the keyword
  score. Mirrors [personal project]'s mockable structured-output pattern.
- **Caching:** store keeps `fit_score`, `fit_reason`, `fit_hash`. Unchanged hash
  → skip re-scoring (no re-billing Gemini).
- **Blend:** `rank_score = 0.5·fit + 0.3·urgency + 0.2·location`.
  `urgency` decays from the deadline (no deadline → neutral-low). `location`
  derives from the existing MA=0 / remote=1 / else=2 rank, inverted to a boost.
- **Graceful degradation (first-class requirement):** no key → keyword floor;
  Gemini failure/timeout on a posting → fall back to that posting's keyword score
  and continue. A single bad score never crashes a run.

### `sheet.py` (replaces `digest.py`)

One worksheet, one row per active posting (`active_records` — `rejected`/`skip`
drop off), sorted by `rank_score` desc. Columns:

`# | Score | Fit | Why (fit_reason) | Role | Company | Location | Deadline |
Days left | Status | Source | Link | First seen`

- **Status read-back:** at run start, read the Status column keyed by `id`, fold
  into the store. Status is a Sheets **data-validation dropdown**:
  `new · interested · applied · interviewing · offer · rejected · skip`.
- **Conditional formatting:** urgency color on Days-left; fit heat scale on Fit.
  Applied once; runs just write values.
- **Library:** `gspread` + `google-auth`, authenticated with a service-account
  JSON (secret), writing to a sheet whose ID is a secret.

## Error handling

- No `GEMINI_API_KEY` → keyword floor, no error.
- Gemini per-posting failure/timeout → keyword fallback for that row, continue.
- Missing Sheets secrets → run fails clearly (Sheet is the whole output) and the
  workflow opens the failure issue.
- Sheets API transient errors → surface via the failure issue; store is still
  committed so no data is lost.

## Testing (network-free; CI stays green with no secrets)

- Keyword scoring: exact fit values for a known profile + posting.
- Blend math: fit/urgency/location weighting produces expected ordering.
- Gemini layer: **mocked** — asserts keyword fallback on error and that caching
  skips unchanged rows.
- `sheet.py`: Sheets client **mocked** — asserts the row model (column order,
  values) and the status read-back merge, without network.
- Existing store/urgency/sanitization tests kept (minus email-specific ones).

## Dependency & secret changes

- **Add:** `gspread`, `google-auth`, `google-generativeai`.
- **Remove:** `markdown` (was only for the email HTML).
- **Add secrets:** `GOOGLE_SERVICE_ACCOUNT_JSON`, `GSHEET_ID`, `GEMINI_API_KEY`.
- **Remove secrets:** `EMAIL_SENDER`, `EMAIL_RECIPIENT`, `EMAIL_PASSWORD`.
  `USAJOBS_*` unchanged.
- **Workflow (`track.yml`):** swap the env block; keep "commit store back" and
  "open issue on failure" steps.

## Docs

`README.md` rewritten: the ranked-sheet model, `profile.toml`, service-account
setup (create service account, share the sheet with its email), and the new
secrets. Roadmap updated (Phase 2 = this pivot; ATS deadline scraping deferred).

## Out of scope (YAGNI)

- Company ATS-page scraping for descriptions/deadlines.
- Multiple worksheet tabs / a bespoke web dashboard.
- Any remaining email path (fully removed; failure = GitHub issue).
