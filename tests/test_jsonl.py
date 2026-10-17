from archiv import jsonl


def test_read_skips_what_is_no_object():
    data = ('\ufeff{"datum": "2026-10-16", "gerichte": [{"name": "Schweinebraten", "preis": "12,50"}]}\n'
            '\n'
            '{"datum": "2026-10-17", "gerich\n'          # cut short while being written
            '[1, 2]\n'
            '{ "gerichte": [{"preis": "12,50", "name": "Schweinebraten"}], "datum": "2026-10-16" }\n'  # same, other spacing
            '{"datum": "2026-10-17"}\n').encode("utf-8")
    rows = jsonl.read(data)
    assert [value["datum"] for _, value in rows] == ["2026-10-16", "2026-10-17"]
    assert rows[0][1]["gerichte"][0]["name"] == "Schweinebraten"


def test_hash_and_fingerprint_ignore_order():
    assert jsonl.line_hash({"a": 1, "b": "ü"}) == jsonl.line_hash({"b": "ü", "a": 1})
    one = jsonl.read(b'{"a": 1}\n{"a": 2}\n')
    two = jsonl.read(b'{"a": 2}\n{"a": 1}\n')
    assert jsonl.fingerprint(one) == jsonl.fingerprint(two)
    assert jsonl.fingerprint(one) != jsonl.fingerprint(jsonl.read(b'{"a": 1}\n'))
