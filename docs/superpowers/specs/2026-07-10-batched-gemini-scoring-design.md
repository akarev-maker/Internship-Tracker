# Batched, budgeted Gemini scoring — design

**Date:** 2026-07-10
**Status:** approved (design review in chat)

## Problem

`score_store` calls Gemini once per posting that needs a fresh fit score,
serially. On the first deployed run (or after a `profile.toml` edit) that is
one network round-trip per active posting — plausibly 50–300 calls.

The user's `GEMINI_API_KEY` is on the **free tier**: ~15 requests/minute,
~1,000 requests/day for flash-lite. That makes the rate limit, not latency,
the bottleneck — so thread-pool parallelism is counterproductive (it converts
slowness into 429 errors). The fix is **fewer requests**: batch postings into
each request, spend them on the most promising postings first, and cap spend
per run.

## Approach

Batch ~10 postings per Gemini request, order candidates by a preliminary
rank so the top of the sheet gets LLM-quality scores first, cap requests per
run, pace calls under the free-tier RPM limit, and stop cleanly on the first
failure. Everything unscored keeps its deterministic keyword score and
self-heals on later runs via the existing `fit_source` retry semantics.

## Interface change

The one contract that moves:

```
# before
gemini_fit(resume, text) -> (score, reason) | None

# after
gemini_fit_batch(resume, items) -> {id: (score, reason)} | None
#   items: list of {"id": str, "text": str}
#   returns None when GEMINI_API_KEY is unset (same convention as before)
#   raises on API/parse failure (caller decides what a failure means)
```

`score_store(store, profile, gemini_fn=..., today=...)` keeps its injected
`gemini_fn` parameter; it now has the batch signature. Tests inject
batch-shaped fakes.

## Components

### `gemini_fit.py`

- `gemini_fit_batch(resume, items)` — builds one prompt containing up to
  `BATCH_SIZE` postings, each labeled with its id. Response schema is a JSON
  array of `{id, score, reason}` (structured output, like today).
- `_parse_batch(text, expected_ids)` — clamps scores to 0–100, maps results
  by id, ignores ids it didn't ask about and malformed entries. Postings
  absent from the response are simply absent from the returned dict.
- **Pacing lives here**: a module-level timestamp throttles consecutive real
  API calls to ≥ `MIN_SECONDS_BETWEEN_CALLS` apart (4.5s ≈ 13/min, under the
  15 RPM limit). Keeping the throttle in the production function keeps
  `score_store` pure and tests sleep-free.
- The single-posting `gemini_fit` is removed (nothing else calls it).
- Client reuse (one `genai.Client` per run) is kept.

### `scoring.py` — `score_store` flow

1. For every active record whose fit is stale or keyword-sourced, compute the
   **keyword score first** — it is both the floor and the priority signal.
   Stamp it (`fit_source="keyword"`) immediately, so a run that never reaches
   Gemini still leaves every record consistently scored.
2. Collect records still wanting Gemini (`fit_source != "gemini"`), sorted by
   preliminary rank — the same `blend(fit, urgency, location)` using the
   keyword fit — descending, so the most promising postings are scored first.
3. Chunk into batches of `BATCH_SIZE` (10), issue at most
   `MAX_GEMINI_REQUESTS_PER_RUN` (30) batch calls (≈300 postings/run;
   ~2.5 min with pacing, inside the 10-min workflow timeout and the
   1,000/day quota).
4. Each successful batch stamps `fit_score`/`fit_reason`/`fit_source="gemini"`
   for the ids it returned. Ids missing from a response keep keyword.
5. On the **first batch exception** (429, malformed JSON, network), log a
   warning and stop issuing batches this run. Remaining records keep their
   keyword floor and are retried on subsequent daily runs.
6. Urgency/location/`rank_score` recompute for all records exactly as today.

Constants (module-level, easy to bump on a paid key):

```
BATCH_SIZE = 10                      # postings per Gemini request
MAX_GEMINI_REQUESTS_PER_RUN = 30     # budget per run
MIN_SECONDS_BETWEEN_CALLS = 4.5      # free tier: stay under 15 RPM
```

(`BATCH_SIZE`/`MAX_GEMINI_REQUESTS_PER_RUN` live in `scoring.py`;
`MIN_SECONDS_BETWEEN_CALLS` lives in `gemini_fit.py` with the throttle.)

## What does not change

- Per-record cache: `fit_hash` (posting text + profile version) and
  `fit_source` semantics — a keyword-sourced fit is retried until Gemini
  answers; a Gemini-sourced fit is final until text/profile changes.
- Rank blending, urgency gating for applied/interviewing/offer, sheet
  writing, store persistence.
- No-key path: `gemini_fit_batch` returns `None`; everything scores keyword,
  exactly like today.
- Records already Gemini-scored consume no budget.

## Error handling summary

| Failure | Behavior |
|---|---|
| No API key | All keyword (unchanged) |
| Batch returns subset of ids | Known ids upgraded; rest keep keyword, retry next run |
| Batch call raises (429/parse/network) | Warn, stop further batches; keyword floor stands |
| Unknown/malformed entries in response | Ignored by `_parse_batch` |

## Testing

- `_parse_batch`: clamping, id mapping, unknown ids ignored, malformed
  entries skipped, empty response.
- `score_store` with fake batch fns:
  - batches are ≤ `BATCH_SIZE` and arrive in priority order (highest
    preliminary rank first);
  - budget enforced when candidates exceed `BATCH_SIZE × budget`;
  - partial response → missing ids keep keyword and are re-sent next run;
  - first failure stops subsequent batches;
  - Gemini-cached records don't consume budget;
  - no-key (`gemini_fn` returning `None`) → all keyword.
- Existing score_store tests updated to the batch signature.
- No test sleeps: the throttle is in `gemini_fit_batch`, which tests never
  call with a real client.

## Out of scope

- Thread-pool/async concurrency (only pays off on a paid tier; the constants
  above are the upgrade knobs).
- Persisting "how many requests used today" across runs (daily cron ×
  30 requests/run stays far under the 1,000/day quota).
