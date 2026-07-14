# ATS board coverage + USAJOBS tuning — design

**Date:** 2026-07-14
**Status:** approved (design review in chat)

## Problem

The tracker's pool is thin: 19 active postings, all from one GitHub feed.
Measured facts driving this design:

- The Summer-2027 feed has 148 active postings and only 2 are security roles,
  with zero near-misses dropped by the title filter — the curated GitHub
  lists are tapped out for security internships. Loosening the filter is a
  dead end and is **out of scope**.
- USAJOBS is wired and authenticated but all four hardcoded queries return
  0 postings today; one of them (`'cybersecurity intern'`) is
  Massachusetts-only, needlessly narrow for federal (often remote/
  multi-location) roles.
- Zero postings currently carry a deadline and zero are MA/remote, so two of
  the three rank axes are flat.

Goal: more relevant postings (new sources), with location data that makes
the MA/remote axis meaningful. Real deadlines remain USAJOBS's job; the
Greenhouse/Lever APIs do not publish close dates (a later enrichment
project may scrape some from job pages).

## Scope

This is sub-project A of the "approach 3" pair agreed in brainstorming:

- **A (this spec):** new ATS-board source + USAJOBS query tuning.
- **B (separate later spec):** link-following enrichment (descriptions +
  scraped deadlines for postings we already track).

## Components

### `companies.toml` — curated board list (new file, repo root)

```toml
# Security-heavy companies whose ATS boards we poll daily.
# Adding a company is one line: {name = "...", slug = "..."}.
# slug = the board identifier in the company's careers URL.

[[greenhouse]]           # boards-api.greenhouse.io/v1/boards/<slug>/jobs
name = "CrowdStrike"
slug = "crowdstrike"
# ... ~25 seeded entries total across both sections

[[lever]]                # api.lever.co/v0/postings/<slug>?mode=json
name = "Example Co"
slug = "exampleco"
```

- Seeded with ~25 security-heavy companies (CrowdStrike, Rapid7, Datadog,
  Cloudflare, Palo Alto Networks, Snyk, SentinelOne, Elastic, HackerOne,
  Okta, Proofpoint, Trail of Bits, …) — exact set fixed at implementation
  time by **verifying every slug resolves** (one-time check script run
  during the build; dead slugs dropped before merge; the check is not part
  of the daily run).
- Parsed with `tomllib` (same pattern as `profile.toml`). Missing file or
  empty sections → the source contributes nothing and logs a warning
  (the tracker must not crash).

### `ats_boards.py` — the new source module (new file)

Public JSON, no API keys:

- Greenhouse: `GET https://boards-api.greenhouse.io/v1/boards/<slug>/jobs`
  → `{"jobs": [{"id", "title", "absolute_url", "location": {"name"},
  "updated_at", ...}]}`
- Lever: `GET https://api.lever.co/v0/postings/<slug>?mode=json`
  → `[{"id", "text" (title), "hostedUrl", "categories": {"location"},
  "createdAt" (ms epoch), "country", ...}]`

Behavior:

- One request per company per day via `util.SESSION` (existing retry
  session, existing `USER_AGENT`).
- **Per-board error isolation**: any exception fetching/parsing one board
  logs `logger.warning` with the company name and continues with the rest —
  same resilience pattern as the existing sources.
- **Inclusion rule**: title contains `"intern"` (case-insensitive), AND title
  matches the **security-career allowlist** — the user's goal is pen
  testing, so the list covers security roles plus the classic entry-path
  roles into the field:
  `security, cyber, pentest, pen test, penetration, red team, blue team,
  appsec, infosec, soc analyst, soc intern, threat, vulnerability, incident,
  forensic, malware, detection, exploit, identity, iam, grc, network,
  it intern, information technology, helpdesk, help desk, system
  administrator, sysadmin, technical support`.
  Matching is **word-boundary** (the `keyword_fit` regex style), never bare
  substring — `soc` as a substring would match "aSSOCiate" and `it` would
  match everything; multi-word entries like `soc analyst` / `it intern`
  avoid this. Generic engineering interns (e.g. "Data Engineering Intern")
  are excluded even at security companies. The GitHub-feed filter is
  unchanged (separate concern, measured as not the bottleneck).
- **Normalization** to the existing posting shape (`sources.py` docstring):
  - `id`: `"ats:" + sha256(f"{ats}:{slug}:{job_id}")[:16]` — stable across
    runs, no duplicates on re-posts.
  - `title`, `company` (the `name` from companies.toml), `link`
    (absolute_url / hostedUrl), `locations` (list of the location string),
    `location_str`, `source`: `"Greenhouse"` or `"Lever"`,
    `term`: `""`, `date_posted`: unix int from `updated_at`/`createdAt`
    (0 if absent/unparseable), `deadline`: `""` (these APIs don't publish
    close dates), `rank`: existing `sources.location_rank(locations)`.
- Exposes `fetch_ats_postings(config_path="companies.toml") -> list[dict]`.

### `sources.py` — one-line integration + USAJOBS tuning

- `fetch_all_postings()` additionally extends with
  `ats_boards.fetch_ats_postings()` (import at top; a failure inside is
  already isolated per board, and a total failure of the module follows the
  same warn-and-continue pattern as other sources).
- `USAJOBS_QUERIES` changes:
  - `("cybersecurity intern", "Massachusetts")` → `("cybersecurity intern",
    None)` (nationwide; federal roles are often remote/multi-location).
  - add `("information security intern", None)`.
  - total stays ≤ 6 queries/run.

## What does not change

- `store.py` merge/dedup (ids are namespaced and stable), scoring, sheet,
  workflow, secrets.
- Deadline handling: ATS postings carry `deadline: ""` honestly; urgency
  stays neutral for them exactly like GitHub-list postings today.

## Error handling summary

| Failure | Behavior |
|---|---|
| companies.toml missing/empty | warn, source yields `[]`, run continues |
| One board 404s / times out / bad JSON | warn with company name, skip it |
| Job entry missing fields | skip that entry (defensive `.get` chains) |
| Both new queries return 0 (seasonal) | normal — nothing special |

## Testing (network-free, existing style)

- Fixture JSON for one Greenhouse board and one Lever board (trimmed real
  response shapes) → parsing + normalization tests (field mapping, id
  format, date conversion, location_str).
- Inclusion rule: intern-title required; allowlist admits "Security
  Engineer Intern", "Penetration Testing Intern", "IT Intern",
  "SOC Analyst Intern"; excludes "Marketing Intern",
  "Data Engineering Intern", and "Associate Product Manager Intern"
  (the `soc`-substring trap); non-intern titles excluded.
- Id stability: same fixture parsed twice → same ids.
- Per-board isolation: a fetcher that raises for one board doesn't lose the
  other board's postings.
- companies.toml loader: missing file → `[]` + no crash.
- USAJOBS query table asserted (nationwide cybersecurity query present,
  MA-scoped one gone).

## Out of scope

- Scraping deadlines/descriptions from job pages (sub-project B).
- Workday boards (no simple public JSON; revisit in B if a target company
  needs it).
- Any change to the security-title filter on the GitHub feeds (measured as
  not the bottleneck).
