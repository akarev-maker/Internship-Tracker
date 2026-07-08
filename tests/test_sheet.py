import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sheet  # noqa: E402
import store as store_mod  # noqa: E402


class FakeWS:
    """Stands in for a gspread worksheet (no network)."""

    def __init__(self, values=None):
        self._values = values or []
        self.cleared = False
        self.updated = None

    def get_all_values(self):
        return self._values

    def clear(self):
        self.cleared = True

    def update(self, range_name=None, values=None, **kwargs):
        self.updated = (range_name, values)


def _rec(pid, rank_score, status="new", title="Intern"):
    return {"id": pid, "title": title, "company": "Acme", "location_str": "Remote",
            "term": "Summer 2027", "source": "test", "link": f"https://x/{pid}",
            "deadline": "", "first_seen": "2026-01-01", "status": status,
            "rank_score": rank_score, "fit_score": 50, "fit_reason": "matched: python"}


def test_build_rows_header_and_ranking():
    store = {"a": _rec("a", 40), "b": _rec("b", 90)}
    rows = sheet.build_rows(store)
    assert rows[0] == sheet.COLUMNS
    assert rows[0][-1] == "ID"
    # highest rank_score first
    id_col = sheet.COLUMNS.index("ID")
    assert rows[1][id_col] == "b"
    assert rows[2][id_col] == "a"


def test_build_rows_excludes_rejected():
    store = {"a": _rec("a", 40, status="rejected"), "b": _rec("b", 90)}
    rows = sheet.build_rows(store)
    assert len(rows) == 2  # header + one active row


def test_apply_status_edits_valid_and_invalid():
    store = {"a": _rec("a", 40)}
    sheet.apply_status_edits(store, {"a": "applied", "ghost": "applied",
                                     "a2": "bogus"})
    assert store["a"]["status"] == "applied"  # valid edit applied
    assert "ghost" not in store            # unknown id ignored


def test_read_status_from_sheet():
    header = sheet.COLUMNS
    row = [""] * len(header)
    row[header.index("ID")] = "a"
    row[header.index("Status")] = "Applied"
    ws = FakeWS([header, row])
    assert sheet.read_status_from_sheet(ws) == {"a": "applied"}


def test_write_sheet_clears_and_updates():
    store = {"b": _rec("b", 90)}
    ws = FakeWS()
    sheet.write_sheet(store, worksheet=ws)
    assert ws.cleared is True
    assert ws.updated is not None
    range_name, values = ws.updated
    assert values[0] == sheet.COLUMNS
