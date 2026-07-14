"""
gemini_fit.py — batched Gemini call that scores postings against the résumé.

Kept in its own module so the only network dependency in scoring lives in one
place and is easy to mock (score_store injects gemini_fit_batch). Returns None
when no key is set so the caller keeps the deterministic keyword scores. Calls
are throttled to stay under the free tier's requests-per-minute limit.
"""

import json
import logging
import os
import time

logger = logging.getLogger("tracker.gemini")

GEMINI_MODEL = "gemini-flash-lite-latest"  # adjust to your account's model id

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


_client = None


def _get_client(key):
    # One client per run — score_store issues one gemini_fit_batch call per
    # batch, and rebuilding the HTTP transport each time wastes time on large
    # first runs.
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=key)
    return _client
