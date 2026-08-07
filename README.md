# 🎯 Internship Tracker

Tracks cybersecurity internships over time and keeps a **live Google Sheet**,
ranked so the top rows are the ones to **apply to first** — weighted toward how
well each role matches *your* skillset, how soon it closes, and whether it's in
Massachusetts / remote.

Runs daily via GitHub Actions.

## How it works

1. `sources.py` fetches postings from curated GitHub internship lists,
   **USAJOBS** (federal roles, which expose a real `ApplicationCloseDate`),
   and **security-company ATS boards** (Greenhouse/Lever) — edit
   `companies.toml` to add or remove companies.
2. `store.py` folds them into a persistent store (`state/applications.json`),
   remembering your **status** and **notes** across runs. That file is your
   private pipeline, so it is gitignored — CI keeps it in the **Actions cache**
   instead of committing it to this public repo.
3. `scoring.py` scores each posting against your `profile.toml`:
   - a deterministic **keyword** floor (always), and
   - a **Gemini** fit score + one-line rationale when `GEMINI_API_KEY` is set
     (batched ~10 postings per request, most promising first, capped per run
     to respect free-tier limits — it overrides the keyword score and adds
     the "Why" column; anything unscored keeps the keyword floor and upgrades
     on later runs).
   Then it blends `rank = 0.5·fit + 0.3·urgency + 0.2·location`.
4. `sheet.py` rewrites the Google Sheet, best-first.

## Your profile

Edit **`profile.toml`** — weighted keywords (`[weights]`) drive the deterministic
score. Higher weight = matters more. Editing the file re-scores every posting once.

The **résumé** is what Gemini matches against, and it's personal, so it is *not*
in this repo: `resume` is left blank and the real text comes from the
`PROFILE_RESUME` secret, which overrides the file when set. Rotating the secret
also re-scores everything once (it's part of the fit-cache key). If you don't
mind publishing yours, filling in `resume` in the file works identically.

## Status

Set status **in the Sheet** (the `Status` column). Each run reads your edits back
into `state/applications.json` before rewriting. Statuses:
`new · interested · applied · interviewing · offer · rejected · skip`
(`rejected`/`skip` drop off the sheet). Postings you've already applied to /
are interviewing for lose their deadline-urgency boost, so untriaged roles
stay at the top. Don't add your own columns — the sheet is rewritten each run.

## Setup

In a GitHub repo, add these **Secrets** (Settings → Secrets and variables → Actions):

| Secret | Value |
|--------|-------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | full JSON key for a Google service account |
| `GSHEET_ID` | the target spreadsheet's ID (from its URL) |
| `PROFILE_RESUME` *(optional)* | your résumé as one prose paragraph — see [Your profile](#your-profile) |
| `GEMINI_API_KEY` *(optional)* | enables Gemini fit scoring + rationale |
| `USAJOBS_API_KEY` *(optional)* | free key from developer.usajobs.gov — deadline-bearing federal postings |
| `USAJOBS_EMAIL` *(optional)* | the email you registered with USAJOBS |

Nothing is read from a config file — every credential comes from the
environment, so a fork of this repo carries none of your access. See
[SECURITY.md](SECURITY.md).

**Google Sheet setup:** create a Google Cloud service account, enable the Google
Sheets API, download its JSON key (→ `GOOGLE_SERVICE_ACCOUNT_JSON`), create a
spreadsheet, and **share it with the service account's email** (Editor). Put the
spreadsheet's ID in `GSHEET_ID`.

## Development

```bash
pip install -r requirements.txt pytest
pytest                 # network-free; needs no secrets
python sources.py      # inspect fetched postings (no store, Sheet, or Gemini)
```

## Roadmap

- **Phase 1:** ingest + persistent store + status ✅
- **Phase 2 (this):** skill-matched ranking + live Google Sheet ✅
- **Phase 3:** ATS-page deadline scraping (Greenhouse/Lever/Workday) for richer deadline coverage
