# Durable store: the Google Sheet, not the Actions cache

Date: 2026-08-07

## Problem

Every posting on the Sheet shows `First seen = 2026-08-07`, and postings seen on
earlier days are gone. The history was not filtered out — it was never loaded.

`state/applications.json` is gitignored, so in CI the store's only home is the
Actions cache (`.github/workflows/track.yml`). Recreating the repo as public on
2026-08-07 destroyed every cache. `load_store` then returned `{}`,
`merge_postings` took its `first_run` branch, and the Sheet was rebuilt from
only what the feeds currently list — with today's date stamped on all of it.

This was not a one-off. Actions caches also evict after 7 days without a hit, so
any quiet week would have produced the same loss.

The accumulate-forever behavior the user wants already exists: `merge_postings`
never deletes, and `write_sheet` writes every non-hidden record. The defect is
purely one of durability.

## Approach

Make the Google Sheet the durable store and demote the Actions cache to a
fast path. The Sheet already survives repo deletion, cache eviction, and CI
outages, already round-trips `Status`, and is the artifact the user actually
looks at — so "on the Sheet" and "in the store" become the same statement, with
no invisible state to drift.

Rejected alternatives: committing `state/applications.json` puts the user's real
pipeline (applied to, rejected from) in a public repo and needs
`contents: write`, undoing the workflow's least-privilege posture; a private
Gist adds a PAT to rotate and a second failure mode for something the Sheet
already does.

## Design

### Rehydration from the Sheet

New `sheet.read_store_from_sheet(worksheets)` parses rows from every given
worksheet (sheet1 and `Archive` — see below) back into records keyed by ID,
locating columns by header name so column order stays free to change. It
restores the durable fields: `id`, `title`, `company`, `location_str`,
`deadline`, `status`, `notes`, `source`, `link`, `first_seen`. `locations` is
recovered by splitting `location_str` on commas (`"Unspecified"` → `[]`), and
`rank` recomputed via `util.location_rank`. `last_seen` defaults to `first_seen`.

`tracker.main` becomes:

```
store = load_store()                             # Actions cache — fast path
store.rehydrate(store, sheet_records)            # Sheet IDs missing from the store
new_ids = store.merge_postings(store, postings)  # refresh volatile fields
sheet.apply_sheet_edits(store, sheet_edits)      # Status/Notes edits win
score_store → save_store → write_sheet
```

`store.rehydrate(store, records)` inserts only IDs the store does not already
have and returns the count restored, for the run's log line. It lives in
`store.py` beside `merge_postings` — it is a store operation; `sheet.py` stays
responsible only for parsing rows into records.

Rehydration fills gaps rather than firing only on a fully empty store, so
partial loss heals the same way. It runs *before* `merge_postings` so the real
`first_seen` is preserved and `first_run` triggers only when the cache and the
Sheet are both empty — no more mass "new" flagging. Status edits are still
applied *after* the merge, for the reason documented in `tracker.py`.

`fit_score` / `fit_hash` / `fit_source` are deliberately not restored:
`score_store` already re-derives any record lacking a fresh Gemini fit, so a
recovery run costs one extra batched pass, inside `MAX_GEMINI_REQUESTS_PER_RUN`.
`term` and `date_posted` are lost on rehydration and refill the next time the
posting appears in a feed; nothing ranks on them (`term` only joins the fit
text via `posting_text`).

### Notes column

`Notes` joins `COLUMNS` after `Status`. `read_status_from_sheet` generalizes to
return both fields; `apply_status_edits` becomes `apply_sheet_edits` and folds
notes in alongside status. Notes are user-authored free text written back with
the existing `raw=True`, which keeps them from being parsed as formulas.

### Archive worksheet

`build_rows` filters through `active_records`, so `rejected` and `skip` records
never reach the Sheet. Once the Sheet is the durable store, that would let a
cache loss resurrect every rejected posting as `new` — the exact annoyance this
work exists to remove.

Hidden records therefore get a second worksheet, `Archive`, in the same column
layout, created on demand if absent. `write_sheet` writes active records to
sheet1 and hidden ones to `Archive`; rehydration reads both. `sheet1` stays the
clean ranked view.

### One-time recovery script

`scripts/seed_sheet_from_store.py`, run locally once, recovers the history still
sitting in the user's local `state/applications.json` (50 records,
`first_seen` back to 2026-08-05). It reads the Sheet, unions it with the local
store — local wins for `first_seen`, `status`, and `notes`; the Sheet wins for
volatile fields, being fresher — and writes the result back through
`sheet.write_sheet`, so archived records land on the `Archive` tab by the same
rule as a normal run. The next CI run reads the restored dates. It is
read-modify-write, never a blind overwrite, and needs
`GOOGLE_SERVICE_ACCOUNT_JSON` and `GSHEET_ID` in the local environment.

### Error handling

A Sheet whose header lacks `ID` already logs a warning and returns no edits;
rehydration follows the same rule and returns no records, leaving the cached
store untouched rather than guessing at column positions. A row with a blank ID
is skipped. A missing `Archive` tab is created, and a failure to create it is
logged and treated as an empty archive — the run still completes and sheet1 is
still written.

## Testing

- `test_sheet.py`: `build_rows` → `read_store_from_sheet` round-trips the
  durable fields; notes round-trip; a header missing `ID` yields no records;
  hidden records route to `Archive` and active ones to sheet1.
- `test_tracker.py`: rehydration on an empty cache preserves `first_seen`;
  rehydration on a partial cache fills only the gaps; a Sheet status beats a
  re-fetched posting; a rejected record in `Archive` does not return as `new`.
- Seed script: the merge rule (local `first_seen`/`status`/`notes` win, Sheet
  volatile fields win) tested directly, no network.

The existing `FakeWS` fake in `tests/test_sheet.py` covers all of it; it grows a
minimal multi-worksheet fake for the `Archive` cases.
