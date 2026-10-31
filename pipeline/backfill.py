"""Phase 1, task 1: load all history into the database.

    python -m pipeline.backfill            # PMD sheets + ERA5 CSVs
    python -m pipeline.backfill --pmd      # only PMD
    python -m pipeline.backfill --weather  # only ERA5 CSVs

Reads the raw HTML cached by `probe_pmd` (no network needed) and the CSVs from
`fetch_weather`. Safe to re-run: everything is upserted.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date

from . import db
from .config import RAW_PMD_DIR, WEATHER_DIR
from .sources import pmd
from .validate import db_lookups, validate_rows


def _month_from_name(name: str) -> date | None:
    """'damsdec15.htm' -> date(2015, 12, 1)"""
    import calendar
    import re

    m = re.fullmatch(r"dams([a-z]{3})(\d{2})\.htm", name.lower())
    if not m:
        return None
    abbr = [a.lower() for a in calendar.month_abbr]
    return date(2000 + int(m.group(2)), abbr.index(m.group(1)), 1)


def load_pmd_files(conn, paths) -> tuple[int, int]:
    """Parse cached PMD sheets into observations. Returns (rows_ok, rows_quarantined)."""
    months = sorted((mo, p) for p in paths if (mo := _month_from_name(p.name)))
    rows, problems = [], []
    for month, path in months:
        parsed = pmd.parse_month_html(path.read_bytes(), month)
        rows += [{"date": r.date, "station_id": r.station_id, "level_ft": r.level_ft,
                  "inflow_cusecs": r.inflow_cusecs, "outflow_cusecs": r.outflow_cusecs,
                  "source": "pmd"} for r in parsed.rows]
        problems += [(f"{path.name}", p) for p in parsed.problems]
    rows.sort(key=lambda r: (r["station_id"], r["date"]))
    dead, prev = db_lookups(conn)
    good, bad = validate_rows(rows, dead_level_lookup=dead, previous_level=prev)
    db.upsert(conn, db.observations, [{**g, "fetched_at": db.utcnow()} for g in good],
              keys=["date", "station_id"])
    db.add_quarantine(conn, bad + problems, source="pmd")
    return len(good), len(bad) + len(problems)


def load_weather_csvs(conn) -> int:
    n = 0
    for path in sorted(WEATHER_DIR.glob("era5_*.csv")):
        with path.open(newline="") as f:
            rows = [{
                "date": date.fromisoformat(r["date"]), "point_id": r["point_id"],
                **{k: (float(r[k]) if r.get(k) not in (None, "") else None)
                   for k in ("tmax_c", "tmin_c", "tmean_c", "precip_mm", "snowfall_cm")},
                "fetched_at": db.utcnow(),
            } for r in csv.DictReader(f)]
        n += db.upsert(conn, db.weather, rows, keys=["date", "point_id"])
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pmd", action="store_true")
    ap.add_argument("--weather", action="store_true")
    args = ap.parse_args(argv)
    do_pmd = args.pmd or not args.weather
    do_weather = args.weather or not args.pmd

    eng = db.get_engine()
    db.init_db(eng)
    with eng.begin() as conn:
        if do_pmd:
            files = list(RAW_PMD_DIR.glob("dams*.htm"))
            if not files:
                print("No cached PMD sheets. Run: python -m pipeline.probe_pmd")
            else:
                ok, bad = load_pmd_files(conn, files)
                db.log_run(conn, "pmd_backfill", "ok" if ok else "failed", ok)
                print(f"PMD: {ok} observation rows upserted from {len(files)} sheets, {bad} quarantined")
        if do_weather:
            n = load_weather_csvs(conn)
            db.log_run(conn, "era5_backfill", "ok" if n else "failed", n)
            print(f"Weather: {n} ERA5 rows upserted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
