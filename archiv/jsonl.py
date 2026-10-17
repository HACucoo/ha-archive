"""JSON Lines files over HTTP (e.g. from HA's www folder, served at /local/): every line is kept for good.

A line is identified by its content, so the file may grow, be rewritten or lose old lines: what the archive
has seen once stays, and a line that comes back unchanged is not stored twice.
"""

import hashlib
import json
import urllib.request


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def read(data: bytes) -> list[tuple[str, dict]]:
    """(hash of the line's content, the line as an object) for each line holding a JSON object.
    Blank lines, broken lines (a write cut short) and anything that is not an object are skipped."""
    rows, seen = [], set()
    for raw in data.decode("utf-8-sig").splitlines():
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        key = line_hash(value)
        if key not in seen:
            seen.add(key)
            rows.append((key, value))
    return rows


def line_hash(value: dict) -> str:
    """Same content, same hash, whatever the key order or spacing in the file."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def fingerprint(rows: list[tuple[str, dict]]) -> str:
    return hashlib.sha256("\n".join(sorted(key for key, _ in rows)).encode()).hexdigest()
