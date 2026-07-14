"""
tracker.py — entry point. Read status edits back from the Sheet, fetch postings,
fold them into the store, score them against your profile, and rewrite the
ranked Google Sheet. Run daily by GitHub Actions. On failure the workflow opens
a GitHub issue (no email).
"""

import logging
import sys
import traceback

import scoring
import sheet
import sources
from user_profile import load_profile
from store import load_store, merge_postings, save_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tracker")


def main():
    prof = load_profile()
    worksheet = sheet.open_worksheet()

    store = load_store()
    # 1) fold the user's in-sheet Status edits back into the durable store.
    sheet.apply_status_edits(store, sheet.read_status_from_sheet(worksheet))

    # 2) ingest + merge.
    postings = sources.fetch_all_postings()
    new_ids = merge_postings(store, postings)

    # 3) score + rank.
    scoring.score_store(store, prof)
    save_store(store)

    # 4) rewrite the ranked sheet.
    sheet.write_sheet(store, worksheet)
    logger.info("Done — %d tracked, %d new.", len(store), len(new_ids))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        logger.critical("Tracker run failed: %s\n%s", exc, traceback.format_exc())
        sys.exit(1)
