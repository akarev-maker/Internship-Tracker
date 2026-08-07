"""
store.py — the persistent applications store (state/applications.json).

This is what makes the tracker a *tracker* rather than a feed: it remembers every
posting it has seen and the status you assign it. New postings enter as "new";
your status/notes on existing ones are preserved across runs.

Set status in the Google Sheet's Status column; sheet.py reads your edits back
into this store at the start of each run. Valid statuses:
    new · interested · applied · interviewing · offer · rejected · skip

The store holds your real pipeline, so it is gitignored and CI keeps it in the
Actions cache rather than committing it to this public repo.
"""

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("tracker.store")

APPLICATIONS_PATH = os.path.join("state", "applications.json")

# Statuses we no longer nag about (deadline reminders, "new" surfacing).
TERMINAL_STATUSES = {"rejected", "skip", "offer"}
# Statuses that still want a deadline nudge (you haven't committed yet).
NEEDS_ACTION_STATUSES = {"new", "interested"}
VALID_STATUSES = NEEDS_ACTION_STATUSES | {"applied", "interviewing"} | TERMINAL_STATUSES


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_store(path=APPLICATIONS_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_store(store, path=APPLICATIONS_PATH):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=1, sort_keys=True)
    except OSError as exc:
        logger.warning("Could not save store to %s: %s", path, exc)


# Fields refreshed from the source each run (status/first_seen/notes are yours).
_VOLATILE = ("title", "company", "link", "locations", "location_str", "source",
             "term", "date_posted", "deadline", "rank")


def merge_postings(store, postings, today=None):
    """Fold freshly fetched postings into the store. Returns the list of new ids.

    First-ever run (empty store) establishes a baseline — nothing is flagged new,
    so you don't get spammed with the entire backlog on day one.
    """
    today = today or _today()
    first_run = not store
    new_ids = []
    for p in postings:
        pid = p["id"]
        if pid in store:
            for k in _VOLATILE:
                if k in p:
                    store[pid][k] = p[k]
            store[pid]["last_seen"] = today
        else:
            record = {k: p.get(k) for k in _VOLATILE}
            record.update(
                {
                    "id": pid,
                    "status": "new",
                    "first_seen": today,
                    "last_seen": today,
                    "notes": "",
                }
            )
            store[pid] = record
            if not first_run:
                new_ids.append(pid)

    if first_run:
        logger.info("First run — baseline of %d posting(s), none flagged new", len(store))
    else:
        logger.info("%d new posting(s) since last run", len(new_ids))
    return new_ids


# Terminal, but still worth showing: an offer belongs on the sheet.
HIDDEN_STATUSES = TERMINAL_STATUSES - {"offer"}


def active_records(store):
    """Records still worth showing (not rejected/skipped)."""
    return [r for r in store.values() if r.get("status") not in HIDDEN_STATUSES]


def archived_records(store):
    """The complement of active_records — rejected/skipped."""
    return [r for r in store.values() if r.get("status") in HIDDEN_STATUSES]


def rehydrate(store, records):
    """Restore records the store is missing. Returns how many were restored.

    The Actions cache is a fast path, not durable storage: it is evicted after
    7 days without a hit and vanishes entirely if the repo is recreated. The
    Google Sheet is what actually survives, so each run reads the Sheet back
    into records and fills whatever the cache lost. Existing entries are never
    touched — the cache's copy is richer (it keeps `term`, `date_posted` and
    the cached Gemini fit, none of which are Sheet columns).

    Must run *before* merge_postings, so a restored posting keeps its real
    first_seen instead of being re-added as new with today's date.
    """
    restored = 0
    for rec in records:
        pid = rec.get("id")
        if pid and pid not in store:
            store[pid] = rec
            restored += 1
    if restored:
        logger.info("Restored %d posting(s) from the Sheet", restored)
    return restored
