"""
tracker.py — entry point. Fetch postings, fold them into the persistent store,
and email an urgency-sorted digest. Run daily by GitHub Actions.
"""

import logging
import sys
import traceback

import digest
import sources
from store import load_store, merge_postings, save_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tracker")


def main():
    postings = sources.fetch_all_postings()

    store = load_store()
    new_ids = merge_postings(store, postings)
    save_store(store)

    digest.send_digest(store, new_ids)
    logger.info("Done — %d tracked, %d new.", len(store), len(new_ids))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        logger.critical("Tracker run failed: %s", exc)
        digest.send_failure_email(f"{exc}\n\n{traceback.format_exc()}")
        sys.exit(1)
