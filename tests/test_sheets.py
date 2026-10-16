import io
from datetime import datetime

import pytest
from openpyxl import Workbook

from archiv import sheets


def workbook(rows_by_tab):
    book = Workbook()
    book.remove(book.active)
    for title, rows in rows_by_tab.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def test_export_url():
    base = "https://docs.google.com/spreadsheets/d/abc_-123/"
    for link in (base + "edit?usp=sharing", base + "edit#gid=5", base + "view"):
        assert sheets.export_url(link) == "https://docs.google.com/spreadsheets/d/abc_-123/export?format=xlsx"
    assert sheets.export_url("http://example/file.xlsx") == "http://example/file.xlsx"


def test_read():
    data = workbook({
        "Daten": [
            [None, "Übersicht"],                      # a title, not the header: only one text cell
            [None, "Datum", "Text", "Text", None],
            [None, datetime(2026, 10, 2), " a b ", "-", 5],
            [],
            [None, datetime(2026, 10, 3, 21, 30), None, "x", None],
        ],
        "Zahlen": [[1, 2], [3, 4]],
    })
    tabs = sheets.read(data)
    assert tabs["Daten"] == [
        (3, {"Datum": "2026-10-02", "Text": "a b", "Text_2": "-", "E": 5}),
        (5, {"Datum": "2026-10-03T21:30:00", "Text_2": "x"}),
    ]
    assert tabs["Zahlen"] == []
    assert sheets.fingerprint(tabs) == sheets.fingerprint(sheets.read(data))


def test_download_rejects_sign_in_page(monkeypatch):
    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(sheets.urllib.request, "urlopen", lambda url, timeout: Answer(b"<!DOCTYPE html>"))
    with pytest.raises(sheets.SheetError, match="shared"):
        sheets.download("https://docs.google.com/spreadsheets/d/x/edit")
    monkeypatch.setattr(sheets.urllib.request, "urlopen", lambda url, timeout: Answer(b"PK\x03\x04rest"))
    assert sheets.download("https://docs.google.com/spreadsheets/d/x/edit").startswith(b"PK")
