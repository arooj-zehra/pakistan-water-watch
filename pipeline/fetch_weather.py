"""Phase 0, task 4: pull daily ERA5 history for every weather point to CSV.

    python -m pipeline.fetch_weather                  # 2010-01-01 .. today-6
    python -m pipeline.fetch_weather --start 2016-01-01
    python -m pipeline.fetch_weather --force          # re-download everything

Writes data/weather/era5_<point>.csv (one file per point). Points whose CSV
already covers the requested start date are skipped, so after a failure you
can simply run it again and only the missing points are fetched.

Open-Meteo's free tier limits calls per minute, and each multi-year request
counts as hundreds of calls, so the script pauses between points and waits
out any "too many requests" reply. Expect a couple of minutes per point.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import date, timedelta

from .config import WEATHER_DIR, WEATHER_POINTS
from .sources import openmeteo

CSV_FIELDS = ["date", "point_id", "tmax_c", "tmin_c", "tmean_c", "precip_mm", "snowfall_cm", "model"]
PAUSE_BETWEEN_POINTS_S = 20


def already_complete(path, start: date, end: date) -> bool:
    """True if the CSV exists, came from the pinned model, and spans
    start..end-7 days (ERA5 lag tolerance). Files from before the model was
    pinned have no `model` column, so they are re-downloaded automatically."""
    if not path.exists():
        return False
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows or rows[0].get("model") != openmeteo.ARCHIVE_MODEL:
        return False
    dates = [r["date"] for r in rows]
    return dates[0] <= start.isoformat() and dates[-1] >= (end - timedelta(days=7)).isoformat()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=(date.today() - timedelta(days=6)).isoformat())
    ap.add_argument("--force", action="store_true", help="re-download points that are already complete")
    args = ap.parse_args(argv)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    failures, fetched = [], 0
    for p in WEATHER_POINTS:
        path = WEATHER_DIR / f"era5_{p.id}.csv"
        if not args.force and already_complete(path, start, end):
            print(f"  {p.id:<13} already complete, skipping")
            continue
        if fetched:
            time.sleep(PAUSE_BETWEEN_POINTS_S)  # stay under the per-minute limit
        print(f"  {p.id:<13} downloading {start}..{end}", flush=True)
        try:
            rows = openmeteo.fetch_archive(p, start, end)
        except Exception as e:
            print(f"  {p.id:<13} FAILED: {e}")
            failures.append(p.id)
            continue
        fetched += 1
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        missing = sum(1 for r in rows if r["tmean_c"] is None)
        print(f"  {p.id:<13} {len(rows):>5} days -> {path.name} ({missing} missing)")

    if failures:
        print(f"\n{len(failures)} point(s) failed: {', '.join(failures)}. "
              "Wait an hour (Open-Meteo hourly limit) and run the same command again.")
        return 1
    print("\nAll weather points complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
