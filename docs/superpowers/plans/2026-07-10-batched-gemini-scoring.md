# Batched, Budgeted Gemini Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-Gemini-call-per-posting scoring path with batched requests (~10 postings each), spent on the most promising postings first under a per-run budget, so a free-tier API key (~15 requests/min) can score a full first run inside the 10-minute workflow timeout.

**Architecture:** `gemini_fit.py` grows a batch endpoint (`gemini_fit_batch`) with structured-array output, per-call throttling, and defensive parsing; `scoring.score_store` becomes two passes — pass 1 stamps a deterministic keyword score on every stale/keyword-sourced record (floor + priority signal), pass 2 sends the best candidates to Gemini in budgeted batches and upgrades whatever comes back. Unscored postings keep the keyword floor and self-heal on later runs via the existing `fit_source` retry semantics.

**Tech Stack:** Python 3.11, `google-genai` (already in requirements.txt), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-10-batched-gemini-scoring-design.md`

## Global Constraints

- Free-tier Gemini: ~15 requests/minute, ~1,000/day. Constants (verbatim from spec): `BATCH_SIZE = 10`, `MAX_GEMINI_REQUESTS_PER_RUN = 30` (both in `scoring.py`), `MIN_SECONDS_BETWEEN_CALLS = 4.5` (in `gemini_fit.py`).
- Tests must be network-free and never sleep (the throttle lives only inside `gemini_fit_batch`, which tests never drive with a real client).
- `gemini_fit_batch` returns `None` when `GEMINI_API_KEY` is unset; raises on API/parse failure.
- Only a Gemini-sourced fit is final: `fit_source == "gemini"` + fresh `fit_hash` skips re-scoring; keyword-sourced fits are retried every run.
- The repo has NO `conftest.py`; every test file does `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` before importing project modules. Follow that pattern.
- Run tests with `python3 -m pytest` from the repo root.

---

### Task 0: Commit the pending audit fixes

The working tree already contains reviewed-but-uncommitted fixes from this morning's audit (suffix-tolerant keyword matching, `fit_source` cache provenance, urgency gating, sheet rewrite-then-trim, `profile.py` → `user_profile.py` rename, dead-config removal). They touch the same files this plan modifies, so they must land as their own commit first.

**Files:**
- Modify: none (commit-only task)

**Interfaces:**
- Consumes: current working tree.
- Produces: a clean baseline commit; later tasks' diffs contain only batching work.

- [ ] **Step 1: Verify the suite is green**

Run: `python3 -m pytest -q`
Expected: `36 passed`

- [ ] **Step 2: Commit everything pending**

```bash
git add -A
git commit -m "fix: audit fixes — fit-source cache provenance, suffix keyword match, urgency gating, safer sheet rewrite, user_profile rename

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 3: Confirm clean tree**

Run: `git status --short`
Expected: empty output

---

### Task 1: `gemini_fit.py` batch endpoint (alongside the old single-call path)

Add the batch prompt, array schema, defensive parser, throttle, and `gemini_fit_batch`. Do NOT delete the old `gemini_fit`/`_parse_fit` yet — `scoring.py` still uses them until Task 2; both paths coexist so the suite stays green after this task.

**Files:**
- Modify: `gemini_fit.py`
- Test: `tests/test_scoring.py` (the `# --- gemini_fit ---` section)

**Interfaces:**
- Consumes: existing `_get_client(key)` and `GEMINI_MODEL` in `gemini_fit.py`.
- Produces: `gemini_fit_batch(resume: str, items: list[dict]) -> dict[str, tuple[int, str]] | None` where each item is `{"id": str, "text": str}`; result maps id → `(score 0-100, reason)`; returns `None` without `GEMINI_API_KEY`; raises `RuntimeError`/`json.JSONDecodeError` on bad responses. Also `_parse_batch(text: str, expected_ids: list[str]) -> dict[str, tuple[int, str]]` and `MIN_SECONDS_BETWEEN_CALLS = 4.5`. Task 2 injects `gemini_fit_batch` as `score_store`'s default.

- [ ] **Step 1: Write the failing tests**

In `tests/test_scoring.py`, add `import pytest` to the imports at the top of the file (after `import sys`), then append to the `# --- gemini_fit ---` section:

```python
def test_parse_batch_maps_clamps_and_filters():
    text = ('[{"id": "a", "score": 130, "reason": "strong"},'
            ' {"id": "b", "score": -5, "reason": "weak"},'
            ' {"id": "ghost", "score": 50, "reason": "not asked"},'
            ' {"id": "c", "reason": "missing score"},'
            ' "junk"]')
    out = gemini_fit._parse_batch(text, ["a", "b", "c"])
    # clamped to 0-100; unknown id and malformed entries dropped;
    # "c" absent -> caller keeps its keyword score
    assert out == {"a": (100, "strong"), "b": (0, "weak")}


def test_parse_batch_strips_markdown_fence():
    fenced = '```json\n[{"id": "a", "score": 7, "reason": "r"}]\n```'
    assert gemini_fit._parse_batch(fenced, ["a"]) == {"a": (7, "r")}


def test_parse_batch_rejects_non_array_and_empty():
    with pytest.raises(RuntimeError):
        gemini_fit._parse_batch('{"id": "a", "score": 7, "reason": "r"}', ["a"])
    with pytest.raises(RuntimeError):
        gemini_fit._parse_batch("", ["a"])


def test_gemini_fit_batch_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = gemini_fit.gemini_fit_batch("resume", [{"id": "a", "text": "t"}])
    assert out is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_scoring.py -q -k parse_batch or gemini_fit_batch`
(Exact: `python3 -m pytest tests/test_scoring.py -q -k "parse_batch or gemini_fit_batch"`)
Expected: 4 FAIL/ERROR with `AttributeError: module 'gemini_fit' has no attribute '_parse_batch'` (and `gemini_fit_batch`)

- [ ] **Step 3: Implement the batch endpoint**

In `gemini_fit.py`: add `import time` to the imports (after `import os`), add `MIN_SECONDS_BETWEEN_CALLS` right after `GEMINI_MODEL`, and append the new code after `_parse_fit` (leave `_SCHEMA`, `_prompt`, `_parse_fit`, `gemini_fit` untouched for now):

```python
MIN_SECONDS_BETWEEN_CALLS = 4.5  # free tier allows ~15 requests/minute

_BATCH_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"id": {"type": "string"},
                       "score": {"type": "integer"},
                       "reason": {"type": "string"}},
        "required": ["id", "score", "reason"],
    },
}


def _batch_prompt(resume, items):
    postings = "\n\n".join(f"POSTING {it['id']}:\n{it['text'].strip()}"
                           for it in items)
    return (
        "You rank cybersecurity internships for a specific candidate. Given "
        "the candidate profile and a list of labeled job postings, return "
        "ONLY a JSON array with exactly one object per posting: "
        '{"id": string (copied exactly from the POSTING label), '
        '"score": int 0-100, "reason": string}. score = how well THAT '
        "posting matches THIS candidate's skills and level (higher = apply "
        "sooner). Use the full 0-100 range; do not cluster on multiples of "
        "5. reason = ONE short sentence citing concrete overlap or gaps. "
        "Do not wrap the JSON in markdown.\n\n"
        f"CANDIDATE PROFILE:\n{resume.strip()}\n\n{postings}"
    )


def _parse_batch(text, expected_ids):
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[len("json"):]
        cleaned = cleaned.strip().rstrip("`").strip()
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise RuntimeError("Gemini response is not a JSON array")
    expected = set(expected_ids)
    out = {}
    for entry in data:
        try:
            pid = str(entry["id"])
            if pid not in expected:
                continue
            score = max(0, min(100, int(entry["score"])))
            out[pid] = (score, str(entry.get("reason", "")))
        except (KeyError, TypeError, ValueError):
            continue  # malformed entry — that posting keeps its keyword score
    return out


_last_call = 0.0


def _throttle():
    # Free-tier RPM limit — space real API calls out; the first call never waits.
    global _last_call
    wait = MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def gemini_fit_batch(resume, items):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    from google.genai import types

    _throttle()
    response = _get_client(key).models.generate_content(
        model=GEMINI_MODEL,
        contents=[_batch_prompt(resume, items)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_BATCH_SCHEMA,
        ),
    )
    return _parse_batch(response.text, [it["id"] for it in items])
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q`
Expected: `40 passed` (36 existing + 4 new)

- [ ] **Step 5: Commit**

```bash
git add gemini_fit.py tests/test_scoring.py
git commit -m "feat: gemini_fit_batch — batched structured scoring with free-tier throttle

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Rewire `score_store` to priority-ordered, budgeted batches

Two-pass rewrite of `score_store` plus a `_set_rank_score` helper. All existing score_store tests move to batch-shaped fakes; three new tests cover priority/budget, stop-on-failure, and partial-response retry.

**Files:**
- Modify: `scoring.py` (constants after `KEYWORD_FIT_TARGET`; replace `score_store`)
- Test: `tests/test_scoring.py` (the `# --- score_store ---` section)

**Interfaces:**
- Consumes: `gemini_fit.gemini_fit_batch(resume, items) -> dict | None` from Task 1; existing `keyword_fit`, `fit_hash`, `posting_text`, `urgency_score`, `location_score`, `blend`, `NEEDS_ACTION_STATUSES`, `active_records`.
- Produces: `score_store(store, profile, gemini_fn=gemini_fit_batch, today=None)` where `gemini_fn` has the batch signature; module constants `BATCH_SIZE = 10`, `MAX_GEMINI_REQUESTS_PER_RUN = 30` (read at call time, so tests may monkeypatch them); `_set_rank_score(rec, today=None)`.

- [ ] **Step 1: Rewrite the score_store test section with batch fakes + new tests**

In `tests/test_scoring.py`, replace every existing test from `test_score_store_keyword_when_no_gemini` to the end of the file with the following (the `PROFILE`, `_seed`, and `_rec` helpers stay as they are):

```python
def _gem(score, reason):
    """Batch-shaped fake: scores every posting it is asked about."""
    def fn(resume, items):
        return {it["id"]: (score, reason) for it in items}
    return fn


def _multi(*pid_rank_pairs, title="Web Application Security Intern"):
    store = {}
    for pid, rank in pid_rank_pairs:
        rec = _rec(title=title, rank=rank)
        rec.update({"id": pid, "status": "new"})
        store[pid] = rec
    return store


def test_score_store_keyword_when_no_gemini():
    store = _seed(title="Web Application Security Intern", rank=0,
                  deadline="")
    scoring.score_store(store, PROFILE, gemini_fn=lambda *_: None, today=date(2026, 1, 1))
    rec = store["a"]
    assert rec["fit_score"] == 33   # only "web application" (5) -> 5/15*100
    assert "web application" in rec["fit_reason"]
    assert rec["fit_source"] == "keyword"
    assert rec["rank_score"] > 0


def test_score_store_gemini_overrides_keyword():
    store = _seed(title="Generic Security Intern")
    scoring.score_store(store, PROFILE, gemini_fn=_gem(77, "strong web"),
                        today=date(2026, 1, 1))
    assert store["a"]["fit_score"] == 77
    assert store["a"]["fit_reason"] == "strong web"
    assert store["a"]["fit_source"] == "gemini"


def test_score_store_falls_back_when_gemini_raises():
    store = _seed(title="Web Application Security Intern")

    def boom(*_):
        raise RuntimeError("gemini down")

    scoring.score_store(store, PROFILE, gemini_fn=boom, today=date(2026, 1, 1))
    assert store["a"]["fit_score"] == 33  # keyword fallback, not a crash
    assert "web application" in store["a"]["fit_reason"]


def test_score_store_caches_unchanged():
    store = _seed(title="Web Application Security Intern", deadline="2026-01-15")
    calls = {"n": 0}

    def counting(resume, items):
        calls["n"] += 1
        return {it["id"]: (80, "match") for it in items}

    # Run with different today values to verify rank_score recomputes
    scoring.score_store(store, PROFILE, gemini_fn=counting, today=date(2026, 1, 1))
    rank_score_1 = store["a"]["rank_score"]
    scoring.score_store(store, PROFILE, gemini_fn=counting, today=date(2026, 1, 8))
    rank_score_2 = store["a"]["rank_score"]

    # fit is cached (gemini_fn called only once), but rank_score recomputed
    assert calls["n"] == 1
    assert rank_score_1 != rank_score_2


def test_score_store_upgrades_keyword_fit_when_gemini_appears():
    # Scored without Gemini (keyword fallback), then Gemini becomes available:
    # the cached keyword fit must be retried and replaced, not kept forever.
    store = _seed(title="Web Application Security Intern")
    scoring.score_store(store, PROFILE, gemini_fn=lambda *_: None, today=date(2026, 1, 1))
    assert store["a"]["fit_source"] == "keyword"
    scoring.score_store(store, PROFILE, gemini_fn=_gem(88, "strong web"),
                        today=date(2026, 1, 1))
    assert store["a"]["fit_score"] == 88
    assert store["a"]["fit_source"] == "gemini"


def test_score_store_applied_gets_no_urgency_boost():
    # The sheet ranks what to APPLY to first — a posting already applied to
    # must not out-rank actionable ones just because its deadline is close.
    common = {"title": "Web Application Security Intern", "deadline": "2026-01-03"}
    fresh = _seed(id="a", **common)
    acted = _seed(id="b", **common)
    acted["b"]["status"] = "applied"
    scoring.score_store(fresh, PROFILE, gemini_fn=None, today=date(2026, 1, 1))
    scoring.score_store(acted, PROFILE, gemini_fn=None, today=date(2026, 1, 1))
    assert fresh["a"]["rank_score"] > acted["b"]["rank_score"]
    # applied item's rank carries zero urgency: fit + location only
    expected = scoring.blend(acted["b"]["fit_score"], 0.0,
                             scoring.location_score(acted["b"]["rank"]))
    assert acted["b"]["rank_score"] == expected


def test_score_store_reprices_when_profile_version_changes():
    store = _seed(title="Web Application Security Intern")
    scoring.score_store(store, PROFILE, gemini_fn=_gem(10, "x"),
                        today=date(2026, 1, 1))
    bumped = dict(PROFILE, version="v2")
    scoring.score_store(store, bumped, gemini_fn=_gem(90, "y"),
                        today=date(2026, 1, 1))
    assert store["a"]["fit_score"] == 90  # re-scored because version changed


def test_score_store_batches_priority_order_and_budget(monkeypatch):
    monkeypatch.setattr(scoring, "BATCH_SIZE", 2)
    monkeypatch.setattr(scoring, "MAX_GEMINI_REQUESTS_PER_RUN", 1)
    # same keyword fit everywhere; location differentiates preliminary rank
    store = _multi(("elsewhere", 2), ("ma", 0), ("remote", 1))
    seen = []

    def fake(resume, items):
        seen.append([it["id"] for it in items])
        return {it["id"]: (90, "gem") for it in items}

    scoring.score_store(store, PROFILE, gemini_fn=fake, today=date(2026, 1, 1))
    # one request (budget=1) of two postings (batch=2), best locations first
    assert seen == [["ma", "remote"]]
    assert store["ma"]["fit_source"] == "gemini"
    assert store["remote"]["fit_source"] == "gemini"
    assert store["elsewhere"]["fit_source"] == "keyword"  # over budget — next run


def test_score_store_stops_after_first_batch_failure(monkeypatch):
    monkeypatch.setattr(scoring, "BATCH_SIZE", 1)
    store = _multi(("a", 0), ("b", 1), ("c", 2))
    calls = {"n": 0}

    def boom(resume, items):
        calls["n"] += 1
        raise RuntimeError("429 rate limited")

    scoring.score_store(store, PROFILE, gemini_fn=boom, today=date(2026, 1, 1))
    assert calls["n"] == 1  # gave up after the first failure
    assert all(r["fit_source"] == "keyword" for r in store.values())


def test_score_store_partial_batch_response_retries_next_run():
    store = _multi(("a", 0), ("b", 0))

    def only_a(resume, items):
        return {"a": (70, "gem")}

    scoring.score_store(store, PROFILE, gemini_fn=only_a, today=date(2026, 1, 1))
    assert store["a"]["fit_source"] == "gemini"
    assert store["b"]["fit_source"] == "keyword"   # missing from the response
    scoring.score_store(store, PROFILE, gemini_fn=_gem(60, "late"),
                        today=date(2026, 1, 1))
    assert store["b"]["fit_source"] == "gemini"    # retried and upgraded
    assert store["a"]["fit_score"] == 70           # already final, not re-sent
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `python3 -m pytest tests/test_scoring.py -q`
Expected: FAILs — `AttributeError: module 'scoring' has no attribute 'BATCH_SIZE'`, plus batch-shaped fakes breaking against the current single-call `score_store` (e.g. `test_score_store_gemini_overrides_keyword` getting a dict where a tuple is expected).

- [ ] **Step 3: Rewrite `score_store` in `scoring.py`**

Add the constants after `KEYWORD_FIT_TARGET = 15.0`:

```python
BATCH_SIZE = 10                   # postings per Gemini request
MAX_GEMINI_REQUESTS_PER_RUN = 30  # request budget per run (free-tier friendly)
```

Replace the entire `score_store` function (and add `_set_rank_score` above it) with:

```python
def _set_rank_score(rec, today=None):
    """rank_score from the record's current fit. Postings already applied to /
    in interview get no urgency boost — the sheet ranks what to *apply* to."""
    urgency = (urgency_score(rec.get("deadline"), today)
               if rec.get("status") in NEEDS_ACTION_STATUSES else 0.0)
    rec["rank_score"] = blend(rec["fit_score"], urgency,
                              location_score(rec.get("rank", 2)))


def score_store(store, profile, gemini_fn=_gemini.gemini_fit_batch, today=None):
    """Score every active record in place.

    Pass 1 stamps a deterministic keyword score on every record whose fit is
    stale or keyword-sourced (the floor and the priority signal). Pass 2
    spends a budgeted number of batched Gemini requests on the most promising
    of those records, best preliminary rank first. Only a Gemini fit is final
    — keyword fits are retried on later runs until Gemini answers, so an
    outage or an exhausted budget self-heals. rank_score is always recomputed
    since urgency changes daily.
    """
    weights = profile.get("weights", {})
    resume = profile.get("resume", "")
    version = profile.get("version", "")

    candidates = []  # (rec, text) whose fit is not (fresh and Gemini-sourced)
    for rec in active_records(store):
        text = posting_text(rec)
        h = fit_hash(text, version)
        cached = rec.get("fit_hash") == h and "fit_score" in rec
        if not (cached and rec.get("fit_source") == "gemini"):
            kw_score, kw_reason = keyword_fit(text, weights)
            rec.update({"fit_score": kw_score, "fit_reason": kw_reason,
                        "fit_hash": h, "fit_source": "keyword"})
            candidates.append((rec, text))
        _set_rank_score(rec, today)

    if gemini_fn is None or not candidates:
        return
    candidates.sort(key=lambda ct: ct[0]["rank_score"], reverse=True)
    candidates = candidates[:BATCH_SIZE * MAX_GEMINI_REQUESTS_PER_RUN]
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start:start + BATCH_SIZE]
        items = [{"id": rec["id"], "text": text} for rec, text in batch]
        try:
            results = {pid: (int(s), str(r))
                       for pid, (s, r) in (gemini_fn(resume, items) or {}).items()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini batch failed — %d posting(s) keep keyword "
                           "scores until the next run: %s", len(items), exc)
            break
        if not results:
            break  # no API key (None) or nothing usable — stop spending budget
        for rec, _text in batch:
            if rec["id"] in results:
                score, reason = results[rec["id"]]
                rec.update({"fit_score": score, "fit_reason": reason,
                            "fit_source": "gemini"})
                _set_rank_score(rec, today)
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest -q`
Expected: `43 passed` (40 from Task 1, minus nothing, plus 3 new score_store tests)

- [ ] **Step 5: Commit**

```bash
git add scoring.py tests/test_scoring.py
git commit -m "feat: score_store — priority-ordered, budgeted Gemini batches

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Remove the single-call Gemini path + docs

Delete the now-unused single-posting code and its tests; update the README so the docs match the behavior.

**Files:**
- Modify: `gemini_fit.py` (delete `_SCHEMA`, `_prompt`, `_parse_fit`, `gemini_fit`)
- Modify: `README.md` ("How it works" bullet 3)
- Test: `tests/test_scoring.py` (delete two obsolete tests)

**Interfaces:**
- Consumes: Task 2's `score_store` (already defaults to `gemini_fit_batch`; nothing references the old path).
- Produces: `gemini_fit.py` exposing only the batch path (`GEMINI_MODEL`, `MIN_SECONDS_BETWEEN_CALLS`, `_BATCH_SCHEMA`, `_batch_prompt`, `_parse_batch`, `_throttle`, `_get_client`, `gemini_fit_batch`).

- [ ] **Step 1: Delete the obsolete tests**

In `tests/test_scoring.py`, delete `test_gemini_parse_fit_clamps_and_extracts` and `test_gemini_fit_returns_none_without_key` (both exercise the single-call path; their batch replacements were added in Task 1).

- [ ] **Step 2: Delete the single-call code**

In `gemini_fit.py`, delete the `_SCHEMA` dict, `_prompt`, `_parse_fit`, and `gemini_fit` functions. Update the module docstring to:

```python
"""
gemini_fit.py — batched Gemini call that scores postings against the résumé.

Kept in its own module so the only network dependency in scoring lives in one
place and is easy to mock (score_store injects gemini_fit_batch). Returns None
when no key is set so the caller keeps the deterministic keyword scores. Calls
are throttled to stay under the free tier's requests-per-minute limit.
"""
```

- [ ] **Step 3: Verify nothing references the deleted names**

Run: `grep -rn "gemini_fit\.gemini_fit\b\|_parse_fit\|_gemini\.gemini_fit\b" --include="*.py" .`
Expected: no matches (note: `_gemini.gemini_fit_batch` in `scoring.py` is fine and won't match these patterns).

- [ ] **Step 4: Update the README**

In `README.md`, replace the bullet:

```markdown
   - a **Gemini** fit score + one-line rationale when `GEMINI_API_KEY` is set
     (it overrides the keyword score and adds the "Why" column).
```

with:

```markdown
   - a **Gemini** fit score + one-line rationale when `GEMINI_API_KEY` is set
     (batched ~10 postings per request, most promising first, capped per run
     to respect free-tier limits — it overrides the keyword score and adds
     the "Why" column; anything unscored keeps the keyword floor and upgrades
     on later runs).
```

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q`
Expected: `41 passed` (43 minus the 2 deleted tests)

- [ ] **Step 6: Commit**

```bash
git add gemini_fit.py README.md tests/test_scoring.py
git commit -m "refactor: drop single-call Gemini path; document batched scoring

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: End-to-end smoke verification

Exercise the full pipeline offline (no network, no sleeps) to see batching, priority, budget, and sheet-write behave together.

**Files:**
- Create: none (throwaway script run from the repo root; do not commit it)

**Interfaces:**
- Consumes: everything above.
- Produces: verified behavior; no code changes.

- [ ] **Step 1: Run the smoke script**

Run from the repo root:

```bash
python3 - <<'EOF'
from datetime import date
import scoring, sheet

profile = {"weights": {"web application": 5, "pentest": 4}, "resume": "r", "version": "v1"}
store = {}
for i in range(25):
    pid = f"p{i:02d}"
    store[pid] = {"id": pid, "title": "Web Application Pentesting Intern",
                  "company": f"C{i}", "location_str": "Remote", "term": "S26",
                  "deadline": "", "rank": i % 3, "status": "new", "source": "t",
                  "link": f"https://x/{pid}", "first_seen": "2026-07-01"}

batches = []
def fake(resume, items):
    batches.append(len(items))
    return {it["id"]: (80, "gem") for it in items}

scoring.score_store(store, profile, gemini_fn=fake, today=date(2026, 7, 10))
gem = sum(1 for r in store.values() if r["fit_source"] == "gemini")
print("batches:", batches, "| gemini-scored:", gem, "of", len(store))
assert batches == [10, 10, 5], batches          # BATCH_SIZE chunks
assert gem == 25                                 # all within budget (30 reqs)

class WS:
    row_count = 100
    def update(self, range_name=None, values=None, raw=None): self.n = len(values)
    def batch_clear(self, ranges): self.trim = ranges
ws = WS(); sheet.write_sheet(store, worksheet=ws)
print("sheet rows:", ws.n, "| trim:", ws.trim)
assert ws.n == 26 and ws.trim == ["27:100"]
print("SMOKE OK")
EOF
```

Expected output ends with `SMOKE OK` (batches `[10, 10, 5]`, 25 of 25 gemini-scored, 26 sheet rows).

- [ ] **Step 2: Full suite one last time**

Run: `python3 -m pytest -q`
Expected: `41 passed`

- [ ] **Step 3: Confirm the branch is clean and log is sane**

Run: `git status --short && git log --oneline -6`
Expected: clean tree; commits from Tasks 0–3 on top of the spec commit.
