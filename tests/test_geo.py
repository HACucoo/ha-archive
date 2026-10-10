import gzip
from datetime import datetime, timedelta, timezone

from archiv import db
from archiv.geo import Places, distance_km


def make_places(tmp_path):
    with gzip.open(tmp_path / "orte.tsv.gz", "wt", encoding="utf-8") as f:
        f.write("49.01513\t12.10161\tRegensburg\tDE\n37.77493\t-122.41942\tSan Francisco\tUS\n41.89193\t12.51133\tRom\tIT\n")
    (tmp_path / "laender.tsv").write_text("DE\tDeutschland\nUS\tVereinigte Staaten\nIT\tItalien\n", encoding="utf-8")
    return Places(tmp_path)


def test_distance():
    assert round(distance_km(48.99, 12.08, 41.89, 12.51)) == 790


def test_nearest(tmp_path):
    places = make_places(tmp_path)
    name, land, km = places.nearest(37.80, -122.44)
    assert (name, land) == ("San Francisco", "Vereinigte Staaten") and km < 5
    assert places.nearest(48.99, 12.08)[0] == "Regensburg"
    assert places.nearest(0.0, 0.0) is None   # middle of the Atlantic


def test_heute_vor(pg, tmp_path):
    db.setup(pg)
    db.set_home(pg, 48.9947, 12.0762)
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    def years_ago(n, days=0):
        return today.replace(year=today.year - n) + timedelta(days=days)
    rows = [
        ("person.stephan", years_ago(7), 37.80, -122.44),     # San Francisco, today 7 years ago
        ("person.stephan", years_ago(7, 0) + timedelta(hours=2), 48.99, 12.08),  # back home later that day: further one wins
        ("person.stephan", years_ago(3), 48.995, 12.077),     # at home: not interesting
        ("person.stephan", years_ago(2, 1), 41.89, 12.51),    # Rome, tomorrow 2 years ago
    ]
    for eid, t, lat, lon in rows:
        pg.execute('INSERT INTO positions (entity_id, "time", name, state, latitude, longitude, source)'
                   " VALUES (%s, %s, 'Stephan', 'x', %s, %s, 'google')", (eid, t, lat, lon))
    pg.commit()
    assert db.fill_place_names(pg, make_places(tmp_path)) == 4
    assert db.fill_place_names(pg, make_places(tmp_path)) == 0
    got = pg.execute("SELECT tag, jahre, ort, land FROM heute_vor ORDER BY tag, jahre").fetchall()
    today_tag = datetime.now(timezone.utc).astimezone().strftime("%m-%d")
    assert (today_tag, 7, "San Francisco", "Vereinigte Staaten") in got
    assert any(r[1] == 2 and r[2] == "Rom" for r in got)
    assert not any(r[1] == 3 for r in got)
