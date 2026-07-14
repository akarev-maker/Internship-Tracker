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
import re
from datetime import date, datetime

import gemini_fit as _gemini
from store import NEEDS_ACTION_STATUSES, active_records

logger = logging.getLogger("tracker.scoring")

FIT_WEIGHT = 0.5
URGENCY_WEIGHT = 0.3
LOCATION_WEIGHT = 0.2
DEADLINE_WINDOW_DAYS = 21
# Matched keyword-weight that counts as a top (100) fit. With weights of 3-5,
# hitting ~3-4 strong terms saturates the score.
KEYWORD_FIT_TARGET = 15.0


def posting_text(rec):
    """The text we have to match against (the fields the record actually
    carries: title, company, term, location)."""
    parts = [rec.get("title", ""), rec.get("company", ""),
             str(rec.get("term", "")), rec.get("location_str", "")]
    return " ".join(p for p in parts if p)


def keyword_fit(text, weights):
    """Deterministic 0-100 fit from weighted keyword overlap + a reason string."""
    low = text.lower()
    # Word-boundary match, tolerating common suffixes so "penetration test"
    # still hits "Penetration Testing" (but "api" never hits "Rapid7").
    matched = [(kw, w) for kw, w in weights.items()
               if re.search(rf"\b{re.escape(kw)}(?:s|es|ed|er|ers|ing)?\b", low)]
    if not matched:
        return 0, "no profile keywords matched"
    total = sum(w for _, w in matched)
    score = int(min(100.0, total / KEYWORD_FIT_TARGET * 100.0))
    terms = ", ".join(kw for kw, _ in sorted(matched, key=lambda t: -t[1]))
    return score, f"matched: {terms}"


def days_until(deadline_iso, today=None):
    if not deadline_iso:
        return None
    try:
        d = datetime.strptime(deadline_iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (d - (today or date.today())).days


def urgency_score(deadline_iso, today=None):
    """0-100. No deadline -> neutral-low (20). 0 days -> 100, window -> 40."""
    days = days_until(deadline_iso, today)
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
    """Score every active record in place.

    Fit is cached by (posting text + profile version), but only a Gemini fit is
    treated as final — a keyword fit is a fallback, so it is retried each run
    until Gemini answers (enabling GEMINI_API_KEY later, or an outage ending,
    upgrades old scores). rank_score is always recomputed: urgency changes
    daily, and postings already applied to / in interview get no urgency boost
    (the sheet ranks what to *apply* to first).
    """
    weights = profile.get("weights", {})
    resume = profile.get("resume", "")
    version = profile.get("version", "")
    for rec in active_records(store):
        text = posting_text(rec)
        h = fit_hash(text, version)
        cached = rec.get("fit_hash") == h and "fit_score" in rec
        if not (cached and rec.get("fit_source") == "gemini"):
            fit = reason = None
            if gemini_fn is not None:
                try:
                    result = gemini_fn(resume, text)
                    if result:
                        fit, reason = int(result[0]), str(result[1])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Gemini fit failed for %s: %s", rec.get("id"), exc)
            source = "gemini" if fit is not None else "keyword"
            if fit is None:
                fit, reason = keyword_fit(text, weights)
            rec.update({"fit_score": fit, "fit_reason": reason,
                        "fit_hash": h, "fit_source": source})
        urgency = (urgency_score(rec.get("deadline"), today)
                   if rec.get("status") in NEEDS_ACTION_STATUSES else 0.0)
        location = location_score(rec.get("rank", 2))
        rec["rank_score"] = blend(rec["fit_score"], urgency, location)
