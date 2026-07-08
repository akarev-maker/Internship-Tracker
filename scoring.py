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

import gemini_fit as _gemini
from store import active_records

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
