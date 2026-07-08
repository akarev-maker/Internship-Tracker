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
