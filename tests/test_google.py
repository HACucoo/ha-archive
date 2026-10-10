from datetime import datetime

from archiv import db, google

ZONES = google.zones_from_states([
    {"entity_id": "zone.home", "attributes": {"latitude": 48.9947, "longitude": 12.0762, "radius": 40}},
    {"entity_id": "zone.wirtshaus", "attributes": {"latitude": 48.9990, "longitude": 12.0780, "radius": 30,
                                                   "friendly_name": "Wirtshaus"}},
    {"entity_id": "zone.weg", "attributes": {"latitude": 0, "longitude": 0, "radius": 10, "passive": True}},
])

EXPORT = {"semanticSegments": [
    {"startTime": "2015-06-01T00:00:00.000+02:00", "endTime": "2015-06-01T08:00:00.000+02:00",
     "visit": {"hierarchyLevel": 0, "topCandidate": {"semanticType": "HOME", "placeId": "h",
               "placeLocation": {"latLng": "48.9943379°, 12.0807075°"}}}},
    {"startTime": "2015-06-01T08:00:00.000+02:00", "endTime": "2015-06-01T08:20:00.000+02:00",
     "activity": {"start": {"latLng": "48.9943379°, 12.0807075°"}, "end": {"latLng": "49.0122°, 12.0743°"},
                  "topCandidate": {"type": "CYCLING"}}},
    {"startTime": "2015-06-01T08:00:00.000+02:00", "endTime": "2015-06-01T10:00:00.000+02:00",
     "timelinePath": [{"point": "49.00°, 12.08°", "time": "2015-06-01T08:10:00.000+02:00"},
                      {"point": "49.0122°, 12.0743°", "time": "2015-06-01T09:00:00.000+02:00"}]},
    {"startTime": "2015-06-01T08:20:00.000+02:00", "endTime": "2015-06-01T17:00:00.000+02:00",
     "visit": {"hierarchyLevel": 0, "topCandidate": {"semanticType": "WORK", "placeId": "w",
               "placeLocation": {"latLng": "49.0122°, 12.0743°"}}}},
    {"startTime": "2015-06-01T18:00:00.000+02:00", "endTime": "2015-06-01T21:00:00.000+02:00",
     "visit": {"hierarchyLevel": 0, "topCandidate": {"semanticType": "UNKNOWN",
               "placeLocation": {"latLng": "48.99901°, 12.07802°"}}}},
    {"startTime": "2015-06-01T19:00:00.000+02:00", "endTime": "2015-06-01T19:30:00.000+02:00",
     "visit": {"hierarchyLevel": 1, "topCandidate": {"semanticType": "UNKNOWN",
               "placeLocation": {"latLng": "10°, 10°"}}}},
    {"startTime": "2015-06-01T22:00:00.000+02:00", "endTime": "2015-06-02T00:00:00.000+02:00",
     "visit": {"hierarchyLevel": 0, "topCandidate": {"semanticType": "UNKNOWN",
               "placeLocation": {"latLng": "48.5°, 11.9°"}}}},
    {"startTime": "2015-06-01T00:00:00.000+02:00", "endTime": "2015-06-01T23:00:00.000+02:00",
     "timelineMemory": {"trip": {}}},
]}


def visit(start, end, kind, place="p-old-home", latlng="48.9943379°, 12.0807075°"):
    return {"startTime": start, "endTime": end, "visit": {"hierarchyLevel": 0, "topCandidate": {
        "semanticType": kind, "placeId": place, "placeLocation": {"latLng": latlng}}}}


def test_labels_carry_over_to_unlabelled_visits():
    days = [f"2019-03-{d:02d}" for d in range(1, 8)]
    export = {"semanticSegments": [
        visit(f"{d}T00:00:00+01:00", f"{d}T07:00:00+01:00", kind)
        for d, kind in zip(days, ["HOME", "UNKNOWN", "HOME", "UNKNOWN", "HOME", "UNKNOWN", "UNKNOWN"])
    ] + [
        # Labelled once: that visit counts, the place does not
        visit("2019-04-01T10:00:00+02:00", "2019-04-01T11:00:00+02:00", "HOME", place="p-once", latlng="48.5°, 11.9°"),
        # After the last HOME label (moved out) the old flat is just another place
        visit("2024-05-01T10:00:00+02:00", "2024-05-01T11:00:00+02:00", "SEARCHED_ADDRESS"),
    ]}
    states = {r[0].strftime("%Y-%m-%d %H"): r[1] for r in google.rows(export, ZONES)}
    assert [states[f"{d} 00"] for d in days[:5]] == ["home"] * 5   # unlabelled 02. and 04. included
    assert states["2019-03-06 00"] == states["2019-03-07 00"] == "anderer Ort"   # after the last label
    assert states["2019-04-01 10"] == "home"
    assert states["2024-05-01 10"] == "anderer Ort"


def test_parse_latlng():
    assert google.parse_latlng("48.9943379°, 12.0807075°") == (48.9943379, 12.0807075)
    assert google.parse_latlng("geo:-33.5,151.2") == (-33.5, 151.2)
    assert google.parse_latlng("") is None


def test_zones():
    assert [z[0] for z in ZONES] == ["Wirtshaus", "home"]
    assert google.zone_at((48.9947, 12.0762), ZONES) == "home"
    assert google.zone_at((48.5, 11.9), ZONES) is None


def test_rows():
    rows = google.rows(EXPORT, ZONES)
    by_time = {r[0].strftime("%H:%M"): r[1] for r in rows if r[0].day == 1}
    assert by_time["00:00"] == "home"            # HOME although outside today's home zone
    assert by_time["08:00"] == "not_home"        # trip start ends the home stay
    assert by_time["08:10"] == "not_home"        # path point between visits
    assert by_time["09:00"] == "Arbeit"          # path point inside the work visit
    assert by_time["18:00"] == "Wirtshaus"       # unlabelled visit inside an HA zone
    assert by_time["22:00"] == "anderer Ort"     # unlabelled visit elsewhere
    assert "19:00" not in by_time                # sub-visits are skipped
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)


def test_import_into_db(pg):
    db.setup(pg)
    rows = google.rows(EXPORT, ZONES)
    # HA's own data starts in the evening: Google rows from then on are left out
    pg.execute("INSERT INTO positions (entity_id, \"time\", name, state, source) VALUES "
               "('person.stephan', '2015-06-01T20:00:00+02:00', 'Stephan', 'Wirtshaus', 'device_tracker.x')")
    count, cutoff = db.replace_google_positions(pg, "person.stephan", "Stephan", rows)
    assert cutoff == datetime.fromisoformat("2015-06-01T20:00:00+02:00")
    assert count == sum(1 for r in rows if r[0] < cutoff)
    # Importing again replaces, never doubles
    db.replace_google_positions(pg, "person.stephan", "Stephan", rows)
    assert pg.execute("SELECT count(*) FROM positions WHERE source = 'google'").fetchone()[0] == count
    stays = pg.execute("SELECT ort FROM aufenthalte ORDER BY beginn").fetchall()
    assert [s[0] for s in stays] == ["Zuhause", "unterwegs", "Arbeit", "Wirtshaus"]
