"""
sheet.py — the ranked Google Sheet (replaces the old email digest).

Reads the user's Status edits back from the sheet into the store, then rewrites
one worksheet, one row per active posting, sorted best-first by rank_score.
Status is the interface; state/applications.json stays the durable store.
"""

import json
import logging
import os

from scoring import _days_until
from store import VALID_STATUSES, active_records

logger = logging.getLogger("tracker.sheet")

COLUMNS = ["#", "Score", "Fit", "Why", "Role", "Company", "Location",
           "Deadline", "Days left", "Status", "Source", "Link", "First seen", "ID"]


def build_rows(store):
    recs = sorted(active_records(store), key=lambda r: r.get("rank_score", 0),
                  reverse=True)
    rows = [list(COLUMNS)]
    for i, r in enumerate(recs, 1):
        days = _days_until(r.get("deadline"))
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
            r.get("source", ""),
            r.get("link", ""),
            r.get("first_seen", ""),
            r.get("id", ""),
        ])
    return rows


def apply_status_edits(store, status_by_id):
    for pid, status in status_by_id.items():
        if pid in store and status in VALID_STATUSES:
            store[pid]["status"] = status


def read_status_from_sheet(worksheet=None):
    ws = worksheet or _open_worksheet()
    values = ws.get_all_values()
    if not values:
        return {}
    header = values[0]
    try:
        id_i = header.index("ID")
        st_i = header.index("Status")
    except ValueError:
        return {}
    out = {}
    for row in values[1:]:
        if len(row) > max(id_i, st_i):
            pid = row[id_i].strip()
            status = row[st_i].strip().lower()
            if pid:
                out[pid] = status
    return out


def write_sheet(store, worksheet=None):
    ws = worksheet or _open_worksheet()
    rows = build_rows(store)
    ws.clear()
    ws.update(range_name="A1", values=rows)
    logger.info("Wrote %d posting(s) to the sheet.", len(rows) - 1)


def _open_worksheet():
    import gspread

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("GSHEET_ID")
    missing = [n for n, v in (("GOOGLE_SERVICE_ACCOUNT_JSON", raw),
                              ("GSHEET_ID", sheet_id)) if not v]
    if missing:
        raise RuntimeError(f"Missing sheet env var(s): {', '.join(missing)}")
    gc = gspread.service_account_from_dict(json.loads(raw))
    return gc.open_by_key(sheet_id).sheet1
