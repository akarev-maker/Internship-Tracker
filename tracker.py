"""
tracker.py — entry point. Read the Sheet back (postings + your Status/Notes
edits), restore whatever the Actions cache lost, fetch new postings, fold them
into the store, score them against your profile, and rewrite the ranked Google
Sheet. Run daily by GitHub Actions. On failure the workflow opens a GitHub issue
(no email).
"""

import logging
import sys
import traceback

import scoring
import sheet
import sources
from user_profile import load_profile
from store import load_store, merge_postings, rehydrate, save_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tracker")


def main():
    prof = load_profile()
    worksheet, archive = sheet.open_worksheets()

    # 1) read the Sheet once: the postings it holds, and the user's Status/Notes
    # edits (applied after the merge below).
    sheet_records, edits = sheet.read_sheet_state([worksheet, archive])

    # 2) restore anything the cache lost, *before* merging. The store lives in
    # the Actions cache, which is evicted after a quiet week and is destroyed
    # if the repo is recreated; the Sheet is what actually persists. Restoring
    # first means a recovered posting keeps its real first_seen instead of
    # being re-added as "new" with today's date.
    store = load_store()
    restored = rehydrate(store, sheet_records)

    # 3) ingest + merge.
    postings = sources.fetch_all_postings()
    new_ids = merge_postings(store, postings)

    # 4) fold the Status/Notes edits in *after* merging, so an edit always wins
    # over the re-fetched posting rather than being overwritten by it.
    sheet.apply_sheet_edits(store, edits)

    # 5) score + rank.
    scoring.score_store(store, prof)
    save_store(store)

    # 6) rewrite the ranked sheet (and the archive tab).
    sheet.write_sheet(store, worksheet, archive)
    logger.info("Done — %d tracked, %d new, %d restored from the Sheet.",
                len(store), len(new_ids), restored)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        logger.critical("Tracker run failed: %s\n%s", exc, traceback.format_exc())
        sys.exit(1)
