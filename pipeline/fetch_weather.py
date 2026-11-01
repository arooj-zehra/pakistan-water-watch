"""Phase 0, task 4: pull daily ERA5 history for every weather point to CSV.

    python -m pipeline.fetch_weather                  # 2010-01-01 .. today-6
    python -m pipeline.fetch_weather --start 2016-01-01
    python -m pipeline.fetch_weather --force          # re-download everything
    python -m pipeline.fetch_weather --update         # append new ERA5 days only (daily job)
    python -m pipeline.fetch_weather --recent         # last 60 days + 16-day forecast (daily job)

Writes data/weather/era5_<point>.csv (one file per point). --recent writes
data/weather/recent_<point>.csv from the forecast API: ERA5 runs ~5 days
behind, so live forecasts take the newest days from here (bias-corrected
against ERA5 by models.data.load_weather), and the forecast days are kept for
later use as features. Points whose CSV
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
RECENT_FIELDS = ["date", "point_id", "kind", "tmax_c", "tmin_c", "tmean_c", "precip_mm", "snowfall_cm", "fetched"]
RECENT_PAST_DAYS = 60
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


def last_date(path) -> date | None:
    if not path.exists():
        return None
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    return date.fromisoformat(rows[-1]["date"]) if rows else None


def update(end: date, fetch=None) -> int:
    """Append ERA5 days after each CSV's last date up to `end` (small requests)."""
    fetch = fetch or openmeteo.fetch_archive
    failures = 0
    for p in WEATHER_POINTS:
        path = WEATHER_DIR / f"era5_{p.id}.csv"
        last = last_date(path)
        if last is None:
            print(f"  {p.id:<13} no CSV yet: run python -m pipeline.fetch_weather first")
            failures += 1
            continue
        if last >= end:
            print(f"  {p.id:<13} up to date ({last})")
            continue
        try:
            rows = [r for r in fetch(p, last + timedelta(days=1), end) if r["tmean_c"] is not None]
        except Exception as e:
            print(f"  {p.id:<13} FAILED: {e}")
            failures += 1
            continue
        with path.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore").writerows(rows)
        print(f"  {p.id:<13} +{len(rows)} days (now to {rows[-1]['date'] if rows else last})")
    return 1 if failures else 0


def recent(fetch=None) -> int:
    """Write recent_<point>.csv: the last RECENT_PAST_DAYS days and 16 forecast days."""
    from datetime import datetime, timezone

    fetch = fetch or openmeteo.fetch_forecast
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    failures = 0
    for p in WEATHER_POINTS:
        try:
            rows = fetch(p, days=16, past_days=RECENT_PAST_DAYS)
        except Exception as e:
            print(f"  {p.id:<13} FAILED: {e}")
            failures += 1
            continue
        path = WEATHER_DIR / f"recent_{p.id}.csv"
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=RECENT_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows({**r, "fetched": stamp} for r in rows)
        n_past = sum(r["kind"] == "past" for r in rows)
        print(f"  {p.id:<13} {n_past} past + {len(rows) - n_past} forecast days -> {path.name}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--end", default=(date.today() - timedelta(days=6)).isoformat())
    ap.add_argument("--force", action="store_true", help="re-download points that are already complete")
    ap.add_argument("--update", action="store_true", help="append new ERA5 days to the existing CSVs")
    ap.add_argument("--recent", action="store_true", help="fetch the last 60 days and the 16-day forecast")
    args = ap.parse_args(argv)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    if args.update or args.recent:
        rc = 0
        if args.update:
            print("ERA5 update:")
            rc |= update(end)
        if args.recent:
            print("Recent days and forecast:")
            rc |= recent()
        return rc
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
