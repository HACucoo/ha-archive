"""Offline place names: nearest town from GeoNames' cities1000 (built into the image by tools/build_geo.py)."""

import csv
import gzip
import math
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "geo"
CELL = 0.5  # degrees per grid cell
MAX_KM = 40  # further from any town: no name


def distance_km(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


class Places:
    def __init__(self, directory: Path = DEFAULT_DIR):
        self.grid: dict[tuple[int, int], list[tuple[float, float, str, str]]] = {}
        with gzip.open(directory / "orte.tsv.gz", "rt", encoding="utf-8", newline="") as f:
            for lat, lon, name, cc in csv.reader(f, delimiter="\t"):
                lat, lon = float(lat), float(lon)
                self.grid.setdefault(self._cell(lat, lon), []).append((lat, lon, name, cc))
        with open(directory / "laender.tsv", encoding="utf-8", newline="") as f:
            self.countries = dict(csv.reader(f, delimiter="\t"))

    @staticmethod
    def _cell(lat, lon):
        return int(math.floor(lat / CELL)), int(math.floor(lon / CELL))

    def nearest(self, lat: float, lon: float) -> tuple[str, str, float] | None:
        """(town, German country name, km) of the nearest town within MAX_KM."""
        cy, cx = self._cell(lat, lon)
        best = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for plat, plon, name, cc in self.grid.get((cy + dy, cx + dx), ()):
                    km = distance_km(lat, lon, plat, plon)
                    if best is None or km < best[2]:
                        best = (name, self.countries.get(cc, cc), km)
        return best if best and best[2] <= MAX_KM else None

    @classmethod
    def available(cls, directory: Path = DEFAULT_DIR) -> bool:
        return (directory / "orte.tsv.gz").exists()
