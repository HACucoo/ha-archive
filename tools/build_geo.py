"""Build the offline place-name table from GeoNames (CC BY 4.0), run once during the image build.

Writes OUT/orte.tsv.gz: lat, lon, name, country code (German name where GeoNames has one)
and OUT/laender.tsv: country code, German country name.
"""

import csv
import gzip
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://download.geonames.org/export/dump/"
# Neighbourhoods, historical and abandoned places would win over the town itself
SKIP_CODES = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"}


def fetch(name: str, target: Path) -> Path:
    path = target / name
    if not path.exists():
        print("download", name, flush=True)
        urllib.request.urlretrieve(BASE + name, path)
    return path


def rows(zip_path: Path, member: str):
    with zipfile.ZipFile(zip_path) as z, z.open(member) as f:
        for line in io.TextIOWrapper(f, encoding="utf-8"):
            yield line.rstrip("\n").split("\t")


def main(out: Path, work: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    cities = {}
    for r in rows(fetch("cities1000.zip", work), "cities1000.txt"):
        if r[7] in SKIP_CODES:
            continue
        cities[r[0]] = [r[4], r[5], r[1], r[8]]

    countries = {}
    country_ids = {}
    with open(fetch("countryInfo.txt", work), encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            r = line.rstrip("\n").split("\t")
            countries[r[0]] = r[4]
            country_ids[r[16]] = r[0]

    german = {}  # geonameid -> (rank, name); preferred names first, colloquial/historic never
    for r in rows(fetch("alternateNamesV2.zip", work), "alternateNamesV2.txt"):
        gid, lang = r[1], r[2]
        if lang != "de" or (gid not in cities and gid not in country_ids):
            continue
        if r[6] == "1" or r[7] == "1":
            continue
        if gid in country_ids:
            # Countries: the short form ("Deutschland", not "Bundesrepublik Deutschland")
            rank = 0 if r[5] == "1" else (1 if r[4] == "1" else 2)
        else:
            rank = 0 if r[4] == "1" else (1 if r[5] != "1" else 2)
        if gid not in german or rank < german[gid][0]:
            german[gid] = (rank, r[3])

    with gzip.open(out / "orte.tsv.gz", "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        for gid, (lat, lon, name, cc) in cities.items():
            w.writerow([lat, lon, german.get(gid, (0, name))[1], cc])
    with open(out / "laender.tsv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        for gid, cc in country_ids.items():
            w.writerow([cc, german.get(gid, (0, countries[cc]))[1]])
    print(len(cities), "places,", len(country_ids), "countries,", sum(1 for g in german if g in cities), "German names")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/geonames"))
