import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sheet  # noqa: E402


class FakeWS:
    """Stands in for a gspread worksheet (no network)."""

    def __init__(self, values=None, row_count=1000):
        self._values = values or []
        self.row_count = row_count
        self.updated = None
        self.batch_cleared = []

    def get_all_values(self):
        return self._values

    def update(self, range_name=None, values=None, **kwargs):
        if len(values) > self.row_count:
            raise RuntimeError("exceeds grid limits")  # what the real API does
        self.updated = (range_name, values)
        self.update_kwargs = kwargs

    def batch_clear(self, ranges):
        self.batch_cleared.extend(ranges)

    def add_rows(self, n):
        self.row_count += n


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


def test_apply_status_edits_rejects_invalid_status_for_known_id():
    store = {"a": _rec("a", 40)}
    sheet.apply_status_edits(store, {"a": "bogus"})
    assert store["a"]["status"] == "new"  # invalid status rejected, original preserved


def test_read_status_from_sheet():
    header = sheet.COLUMNS
    row = [""] * len(header)
    row[header.index("ID")] = "a"
    row[header.index("Status")] = "Applied"
    ws = FakeWS([header, row])
    assert sheet.read_status_from_sheet(ws) == {"a": "applied"}


def test_read_status_from_sheet_survives_column_reorder():
    # Swap Status and ID column positions to test header name lookup, not index
    reordered_header = list(sheet.COLUMNS)
    id_idx = reordered_header.index("ID")
    status_idx = reordered_header.index("Status")
    reordered_header[id_idx], reordered_header[status_idx] = reordered_header[status_idx], reordered_header[id_idx]

    # Build row with values in reordered positions
    row = [""] * len(reordered_header)
    row[reordered_header.index("ID")] = "a"
    row[reordered_header.index("Status")] = "Applied"

    ws = FakeWS([reordered_header, row])
    assert sheet.read_status_from_sheet(ws) == {"a": "applied"}


def test_write_sheet_updates_then_trims_leftover_rows():
    store = {"b": _rec("b", 90)}
    ws = FakeWS(row_count=1000)
    sheet.write_sheet(store, worksheet=ws)
    assert ws.updated is not None
    range_name, values = ws.updated
    assert values[0] == sheet.COLUMNS
    # raw=True keeps untrusted posting/LLM text from being parsed as formulas
    assert ws.update_kwargs.get("raw") is True
    # header + 1 posting = 2 rows written; everything below is trimmed,
    # but only AFTER the new data is in place (no clear-before-write window)
    assert ws.batch_cleared == ["3:1000"]


def test_write_sheet_skips_trim_when_nothing_below():
    store = {"b": _rec("b", 90)}
    ws = FakeWS(row_count=2)
    sheet.write_sheet(store, worksheet=ws)
    assert ws.updated is not None
    assert ws.batch_cleared == []


# --- durable read-back ------------------------------------------------------
def _written(ws):
    """The rows a FakeWS was last written with."""
    return ws.updated[1]


def test_round_trip_preserves_durable_fields():
    """The whole point: what write_sheet puts on the Sheet, read_store_from_sheet
    must give back — otherwise a lost Actions cache loses the history."""
    rec = _rec("a", 90, status="applied")
    rec.update({"first_seen": "2026-08-05", "notes": "referred by X",
                "deadline": "2026-09-01", "company": "Cloudflare",
                "location_str": "Boston, MA"})
    ws = FakeWS()
    sheet.write_sheet({"a": rec}, worksheet=ws)

    back = sheet.read_store_from_sheet(FakeWS(_written(ws)))
    assert len(back) == 1
    got = back[0]
    for field in ("id", "title", "company", "location_str", "term", "deadline",
                  "status", "notes", "source", "link", "first_seen"):
        assert got[field] == rec[field], f"{field} did not survive the round trip"
    assert got["rank"] == 0, "rank must be recomputed from the Location cell"


def test_round_trip_restores_first_seen_not_today():
    rec = _rec("a", 90)
    rec["first_seen"] = "2026-08-05"
    ws = FakeWS()
    sheet.write_sheet({"a": rec}, worksheet=ws)
    assert sheet.read_store_from_sheet(FakeWS(_written(ws)))[0]["first_seen"] == "2026-08-05"


def test_read_back_skips_blank_ids_and_short_rows():
    header = list(sheet.COLUMNS)
    blank = [""] * len(header)
    short = ["1", "2"]
    ws = FakeWS([header, blank, short])
    assert sheet.read_store_from_sheet(ws) == []


def test_read_back_without_id_column_returns_nothing():
    """A mangled header must not be guessed at — better to keep the cached
    store than to invent records from unknown columns."""
    header = [c for c in sheet.COLUMNS if c != "ID"]
    ws = FakeWS([header, ["x"] * len(header)])
    records, edits = sheet.read_sheet_state(ws)
    assert records == [] and edits == {}


def test_empty_worksheet_is_not_a_mangled_header(caplog):
    """A freshly created Archive tab reads back as [[]]. That is an empty
    sheet, not a broken one — warning about lost edits would be a lie."""
    for values in ([], [[]], [["", "  ", ""]]):
        records, edits = sheet.read_sheet_state(FakeWS(values))
        assert records == [] and edits == {}
    assert "missing the ID column" not in caplog.text


def test_read_back_defaults_unknown_status_to_new():
    header = list(sheet.COLUMNS)
    row = [""] * len(header)
    row[header.index("ID")] = "a"
    row[header.index("Status")] = "bogus"
    assert sheet.read_store_from_sheet(FakeWS([header, row]))[0]["status"] == "new"


def test_notes_round_trip_and_apply():
    header = list(sheet.COLUMNS)
    row = [""] * len(header)
    row[header.index("ID")] = "a"
    row[header.index("Status")] = "Applied"
    row[header.index("Notes")] = "phone screen 8/12"

    _records, edits = sheet.read_sheet_state(FakeWS([header, row]))
    store = {"a": _rec("a", 40)}
    sheet.apply_sheet_edits(store, edits)
    assert store["a"]["status"] == "applied"
    assert store["a"]["notes"] == "phone screen 8/12"


def test_rejected_records_go_to_the_archive_tab():
    store = {"a": _rec("a", 40, status="rejected"), "b": _rec("b", 90)}
    main, arch = FakeWS(), FakeWS()
    sheet.write_sheet(store, worksheet=main, archive=arch)

    id_col = sheet.COLUMNS.index("ID")
    assert [r[id_col] for r in _written(main)[1:]] == ["b"]
    assert [r[id_col] for r in _written(arch)[1:]] == ["a"]


def test_archive_read_back_keeps_rejected_status():
    """A rejected posting must not come back as "new" after a cache loss."""
    store = {"a": _rec("a", 40, status="rejected")}
    main, arch = FakeWS(), FakeWS()
    sheet.write_sheet(store, worksheet=main, archive=arch)

    back = sheet.read_store_from_sheet([FakeWS(_written(main)), FakeWS(_written(arch))])
    assert [r["status"] for r in back] == ["rejected"]


def test_write_sheet_grows_the_grid_for_long_lists():
    """With the security filter gone the list is ~1800 rows, and the API
    rejects writes past the grid — the sheet must be grown first."""
    store = {f"p{i}": _rec(f"p{i}", i) for i in range(1500)}
    ws = FakeWS(row_count=1000)
    sheet.write_sheet(store, worksheet=ws)  # must not raise "exceeds grid limits"
    assert len(_written(ws)) == 1501
    assert ws.row_count >= 1501


def test_write_sheet_survives_a_failing_archive_tab():
    """sheet1 is the run's purpose; a broken archive must not fail the run."""

    class BrokenWS(FakeWS):
        def update(self, *a, **kw):
            raise RuntimeError("archive is broken")

    main = FakeWS()
    sheet.write_sheet({"b": _rec("b", 90)}, worksheet=main, archive=BrokenWS())
    assert main.updated is not None
