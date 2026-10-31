"""Phase 0, task 4: pull daily ERA5 history for every weather point to CSV.

    python -m pipeline.fetch_weather                  # 2010-01-01 .. today-6
    python -m pipeline.fetch_weather --start 2016-01-01

Writes data/weather/era5_<point>.csv (one file per point). Re-running
overwrites, so it is safe to repeat.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta

from .config import WEATHER_DIR, WEATHER_POINTS
from .sources import openmeteo

CSV_FIELDS = ["date", "point_id", "tmax_c", "tmin_c", "tmean_c", "precip_mm", "snowfall_cm"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=(date.today() - timedelta(days=6)).isoformat())
    args = ap.parse_args(argv)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for p in WEATHER_POINTS:
        try:
            rows = openmeteo.fetch_archive(p, start, end)
        except Exception as e:
            print(f"  {p.id:<13} FAILED: {e}")
            failures += 1
            continue
        path = WEATHER_DIR / f"era5_{p.id}.csv"
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        missing = sum(1 for r in rows if r["tmean_c"] is None)
        print(f"  {p.id:<13} {len(rows):>5} days -> {path.name} ({missing} missing)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
