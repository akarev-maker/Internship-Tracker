"""
seed_sheet_from_store.py — one-time recovery: push a local applications store
up to the Google Sheet.

Why this exists: the store used to live only in the Actions cache, and
recreating the repo destroyed it. CI then rebuilt the Sheet from scratch and
stamped today's date on every posting. If your local state/applications.json
still holds the real history, this restores it — the Sheet is now the durable
store, so the next CI run reads these dates and statuses back.

Read-modify-write, never a blind overwrite: it reads what is on the Sheet,
unions it with the local store, and writes the result. Your local store wins for
the fields only you can know (first_seen, status, notes); the Sheet wins for
volatile fields, being the fresher fetch.

Usage:
    export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)"
    export GSHEET_ID=...
    python scripts/seed_sheet_from_store.py [--dry-run]
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scoring  # noqa: E402
import sheet  # noqa: E402
from store import load_store  # noqa: E402
from user_profile import load_profile  # noqa: E402

logger = logging.getLogger("tracker.seed")

# What the local store knows and the Sheet cannot: your pipeline and the real
# first-seen date. Everything else on the Sheet is a fresher fetch, so it wins.
_LOCAL_WINS = ("first_seen", "status", "notes")


def merge_local_into_sheet(sheet_records, local_store):
    """Union of both, as a store dict. Local wins for _LOCAL_WINS; the Sheet
    wins for the volatile fields it carries."""
    merged = {}
    for rec in sheet_records:
        merged[rec["id"]] = dict(rec)

    for pid, local in local_store.items():
        if pid not in merged:
            merged[pid] = dict(local)
            continue
        for field in _LOCAL_WINS:
            value = local.get(field)
            if not value:
                continue
            if field == "first_seen":
                # Keep the earlier date — that is what "first seen" means.
                existing = merged[pid].get(field) or ""
                if existing and existing <= value:
                    continue
            merged[pid][field] = value
    return merged


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing the Sheet")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    local = load_store()
    if not local:
        logger.error("No local store found — nothing to seed.")
        return 1
    logger.info("Local store: %d posting(s)", len(local))

    worksheet, archive = sheet.open_worksheets()
    sheet_records = sheet.read_store_from_sheet([worksheet, archive])
    logger.info("Sheet currently holds: %d posting(s)", len(sheet_records))

    merged = merge_local_into_sheet(sheet_records, local)
    on_sheet = {r["id"] for r in sheet_records}
    added = len(set(merged) - on_sheet)
    redated = sum(
        1 for r in sheet_records
        if (local.get(r["id"], {}).get("first_seen") or "") < (r.get("first_seen") or "")
        and local.get(r["id"], {}).get("first_seen")
    )
    logger.info("Merged: %d total, %d added from local, %d with an earlier "
                "first_seen restored", len(merged), added, redated)

    if args.dry_run:
        logger.info("--dry-run: not writing.")
        return 0

    # Score before writing so restored rows carry a rank, matching a normal run.
    scoring.score_store(merged, load_profile())
    sheet.write_sheet(merged, worksheet, archive)
    logger.info("Done. The next CI run will read this back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
