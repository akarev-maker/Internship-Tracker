# Security

## Reporting

Found something? Open a [security advisory](../../security/advisories/new) rather
than a public issue.

## What this project trusts, and what it doesn't

The tracker reads job postings from third-party feeds (curated GitHub lists,
USAJOBS, and public Greenhouse/Lever boards). **Anyone can publish a job
posting**, so every field that arrives from a feed — title, company, location,
link — is treated as untrusted input.

| Boundary | Handling |
|---|---|
| Posting text → Google Sheet | `strip_html()` at ingestion; the sheet is written with `raw=True`, so a title starting with `=` lands as text, not a formula |
| Posting `url` → Link column | `safe_url()` drops anything that isn't `http(s)`, so no `javascript:`/`data:` URL reaches a cell |
| Posting text → Gemini prompt | `posting_text()` collapses newlines (a posting can't forge the `POSTING <id>:` delimiters) and caps length; `_parse_batch()` discards any id it didn't ask about, so a crafted posting can't score a different posting |
| Sheet `Status` column → store | only values in `VALID_STATUSES` are accepted |
| Feed availability | each source and each ATS board is isolated — one failure logs a warning and the run continues |

## Credentials

No credential is ever read from a file in this repo. Everything comes from the
environment, supplied as GitHub Actions secrets: `GOOGLE_SERVICE_ACCOUNT_JSON`,
`GSHEET_ID`, `PROFILE_RESUME`, `GEMINI_API_KEY`, `USAJOBS_API_KEY`,
`USAJOBS_EMAIL`. `.env` is gitignored.

The Google service account should be scoped to the **one spreadsheet** you
shared with it — it has no other access to your Drive.

## Private data

Two things are deliberately kept out of the repo:

- **`state/applications.json`** — your live pipeline (which roles you applied to,
  were rejected from, your notes). Gitignored; CI persists it in the Actions
  cache. Caches evict after 7 days without a run, which resets the "new
  posting" baseline; your statuses survive because they round-trip through the
  Sheet.
- **Your résumé** — supplied via the `PROFILE_RESUME` secret, not `profile.toml`.

## Supply chain

- GitHub Actions are pinned to full commit SHAs, not tags.
- Python dependencies carry version floors *and* major-version ceilings.
- Dependabot proposes weekly updates to both.
- Workflow tokens are least-privilege: `tests.yml` is `contents: read` (it runs
  on fork pull requests); `track.yml` is `contents: read` + `issues: write`.
