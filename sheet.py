"""
sheet.py — the ranked Google Sheet, and the tracker's durable store.

Two jobs. It rewrites the sheet each run (one row per posting, best-first by
rank_score), and it reads that sheet back into records at the start of the next
run. The read-back is what makes the history durable: state/applications.json
lives in the Actions cache, which is evicted after 7 days without a hit and is
destroyed outright if the repo is recreated. The Sheet outlives all of that, so
it — not the cache — is the system of record.

Active postings go to the first worksheet; rejected/skipped ones go to an
`Archive` tab. Both are read back, so a lost cache cannot resurrect a posting
you already rejected.
"""

import json
import logging
import os

from scoring import days_until
from store import (VALID_STATUSES, active_records, archived_records)
from util import location_rank

logger = logging.getLogger("tracker.sheet")

COLUMNS = ["#", "Score", "Fit", "Why", "Role", "Company", "Location",
           "Deadline", "Days left", "Status", "Notes", "Source", "Link",
           "First seen", "ID"]

ARCHIVE_TITLE = "Archive"

# Sheet column -> record field, for the read-back. Only durable fields appear:
# fit_score/fit_hash/fit_source are deliberately omitted so score_store
# re-derives them (it already retries any non-Gemini fit), and term/date_posted
# have no column and refill the next time the posting shows up in a feed.
_COLUMN_FIELDS = {
    "Role": "title",
    "Company": "company",
    "Location": "location_str",
    "Deadline": "deadline",
    "Status": "status",
    "Notes": "notes",
    "Source": "source",
    "Link": "link",
    "First seen": "first_seen",
}


def _rows_for(recs):
    recs = sorted(recs, key=lambda r: r.get("rank_score", 0), reverse=True)
    rows = [list(COLUMNS)]
    for i, r in enumerate(recs, 1):
        days = days_until(r.get("deadline"))
        rows.append([
            i,
            r.get("rank_score", 0),
            r.get("fit_score", 0),
            r.get("fit_reason", ""),
            r.get("title", ""),
            r.get("company", ""),
            r.get("location_str", ""),
            r.get("deadline", ""),
            "" if days is None else days,
            r.get("status", "new"),
            r.get("notes", ""),
            r.get("source", ""),
            r.get("link", ""),
            r.get("first_seen", ""),
            r.get("id", ""),
        ])
    return rows


def build_rows(store):
    return _rows_for(active_records(store))


def build_archive_rows(store):
    return _rows_for(archived_records(store))


def apply_sheet_edits(store, edits_by_id):
    """Fold the user's in-sheet Status/Notes edits into the store."""
    for pid, edit in edits_by_id.items():
        if pid not in store:
            continue
        status = edit.get("status", "")
        if status in VALID_STATUSES:
            store[pid]["status"] = status
        if "notes" in edit:
            store[pid]["notes"] = edit["notes"]


def apply_status_edits(store, status_by_id):
    """Status-only form of apply_sheet_edits (kept for callers passing a plain
    {id: status} mapping)."""
    apply_sheet_edits(store, {pid: {"status": s} for pid, s in status_by_id.items()})


def _locations_from(location_str):
    """Recover a locations list from the joined Location cell. Only `rank` is
    derived from it; location_str itself is stored verbatim, so an imperfect
    split ("Boston, MA" -> ["Boston", "MA"]) costs nothing — location_rank
    tokenizes on commas anyway."""
    if not location_str or location_str == "Unspecified":
        return []
    return [part.strip() for part in location_str.split(",") if part.strip()]


def _parse_values(values):
    """One worksheet's raw rows -> ([records], {id: {status, notes}})."""
    # A blank header row means an empty worksheet (a freshly created Archive
    # tab reads back as [[]] rather than []), not a mangled one — say nothing.
    if not values or not any(h.strip() for h in values[0]):
        return [], {}
    header = values[0]
    if "ID" not in header:
        logger.warning(
            "Sheet header is missing the ID column — skipping read-back; any "
            "Status/Notes edits made in the sheet will be lost when it is "
            "rewritten this run, and postings it holds cannot be restored."
        )
        return [], {}
    idx = {name: header.index(name) for name in header}
    id_i = idx["ID"]

    records, edits = [], {}
    for row in values[1:]:
        if len(row) <= id_i:
            continue
        pid = row[id_i].strip()
        if not pid:
            continue

        def cell(column, _row=row):
            i = idx.get(column)
            return _row[i].strip() if i is not None and len(_row) > i else ""

        status = cell("Status").lower()
        edit = {"status": status}
        if "Notes" in idx:
            edit["notes"] = cell("Notes")
        edits[pid] = edit

        rec = {field: cell(column) for column, field in _COLUMN_FIELDS.items()
               if column in idx}
        rec["id"] = pid
        rec["status"] = status if status in VALID_STATUSES else "new"
        rec.setdefault("notes", "")
        rec["locations"] = _locations_from(rec.get("location_str", ""))
        rec["rank"] = location_rank(rec["locations"])
        # last_seen has no column; first_seen is the safe floor.
        rec["last_seen"] = rec.get("first_seen", "")
        records.append(rec)
    return records, edits


def _as_list(worksheets):
    if worksheets is None:
        return [open_worksheet()]
    if isinstance(worksheets, (list, tuple)):
        return [ws for ws in worksheets if ws is not None]
    return [worksheets]


def read_sheet_state(worksheets=None):
    """Read every worksheet once -> ([records], {id: {status, notes}}).

    One pass, because each get_all_values is a network round trip and the
    records and the edits come from the same cells.
    """
    all_records, all_edits = [], {}
    for ws in _as_list(worksheets):
        try:
            values = ws.get_all_values()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read a worksheet: %s", exc)
            continue
        records, edits = _parse_values(values)
        all_records.extend(records)
        all_edits.update(edits)
    return all_records, all_edits


def read_store_from_sheet(worksheets=None):
    return read_sheet_state(worksheets)[0]


def read_status_from_sheet(worksheets=None):
    return {pid: edit.get("status", "")
            for pid, edit in read_sheet_state(worksheets)[1].items()}


def _write_rows(ws, rows):
    # Overwrite in place, then trim leftover rows from a previously longer
    # sheet — never blank the sheet before the new data is confirmed written.
    # raw=True keeps untrusted posting/LLM text from being parsed as formulas.
    ws.update(range_name="A1", values=rows, raw=True)
    if ws.row_count > len(rows):
        ws.batch_clear([f"{len(rows) + 1}:{ws.row_count}"])


def write_sheet(store, worksheet=None, archive=None):
    ws = worksheet or open_worksheet()
    rows = build_rows(store)
    _write_rows(ws, rows)
    logger.info("Wrote %d posting(s) to the sheet.", len(rows) - 1)

    if archive is not None:
        archived = build_archive_rows(store)
        try:
            _write_rows(archive, archived)
            logger.info("Wrote %d archived posting(s).", len(archived) - 1)
        except Exception as exc:  # noqa: BLE001
            # The archive is a durability backstop, not the run's purpose —
            # sheet1 is already written, so log and finish.
            logger.warning("Could not write the %s tab: %s", ARCHIVE_TITLE, exc)


def open_worksheet():
    return open_worksheets()[0]


def open_worksheets():
    """(active worksheet, archive worksheet). The archive is created on demand;
    if it cannot be opened or created the run continues without it (archived
    postings then live only in the cache)."""
    import gspread

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("GSHEET_ID")
    missing = [n for n, v in (("GOOGLE_SERVICE_ACCOUNT_JSON", raw),
                              ("GSHEET_ID", sheet_id)) if not v]
    if missing:
        raise RuntimeError(f"Missing sheet env var(s): {', '.join(missing)}")
    gc = gspread.service_account_from_dict(json.loads(raw))
    spreadsheet = gc.open_by_key(sheet_id)

    archive = None
    try:
        archive = spreadsheet.worksheet(ARCHIVE_TITLE)
    except Exception:  # noqa: BLE001  (gspread.WorksheetNotFound and friends)
        try:
            archive = spreadsheet.add_worksheet(
                title=ARCHIVE_TITLE, rows=200, cols=len(COLUMNS))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not open or create the %s tab: %s",
                           ARCHIVE_TITLE, exc)
    return spreadsheet.sheet1, archive
