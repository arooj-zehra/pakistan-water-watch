"""Phase 0, task 1: how much PMD history actually exists?

Loops over every month from 2010 to now, downloads each monthly sheet (once:
raw HTML is cached in data/raw/pmd/ and committed), parses it, and writes
data/inventory/pmd_coverage.csv with days filled per station per month.

    python -m pipeline.probe_pmd                 # full run (~7 min, polite)
    python -m pipeline.probe_pmd --start 2020-01 # partial
    python -m pipeline.probe_pmd --offline       # re-parse cached HTML only
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date

from .config import INVENTORY_DIR, PMD_FIRST_MONTH, RAW_PMD_DIR
from .sources import pmd

COVERAGE_CSV = INVENTORY_DIR / "pmd_coverage.csv"
FIELDS = [
    "month", "status", "url", "days_in_month",
    "tarbela_days", "mangla_days", "header_month_ok", "problems",
]


def _looks_like_sheet(html: bytes) -> bool:
    t = html[:20000].upper()
    return b"TARBELA" in t and b"MANGLA" in t


def fetch_month(month: date, *, refresh_recent: bool = True, offline: bool = False) -> tuple[str, str, bytes | None]:
    """Return (status, url, html). Uses the cache unless the month is recent."""
    path = pmd.raw_path(month)
    today = date.today()
    is_recent = (today.year * 12 + today.month) - (month.year * 12 + month.month) <= 1
    if path.exists() and not (refresh_recent and is_recent and not offline):
        return "cached", "", path.read_bytes()
    if offline:
        return "not_cached", "", None

    from . import http  # imported lazily so --offline works without network deps

    last_status = "error"
    for url in pmd.candidate_urls(month):
        try:
            r = http.get(url)
        except Exception as e:  # network error: report, keep going
            last_status = f"error:{type(e).__name__}"
            continue
        if r.status_code == 200 and _looks_like_sheet(r.content):
            RAW_PMD_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            return "ok", url, r.content
        last_status = f"http_{r.status_code}" if r.status_code != 200 else "not_a_sheet"
    return last_status, pmd.candidate_urls(month)[0], None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default=PMD_FIRST_MONTH.strftime("%Y-%m"), help="YYYY-MM")
    ap.add_argument("--end", default=date.today().strftime("%Y-%m"), help="YYYY-MM")
    ap.add_argument("--offline", action="store_true", help="only parse cached HTML")
    args = ap.parse_args(argv)

    start = date.fromisoformat(args.start + "-01")
    end = date.fromisoformat(args.end + "-01")
    months = list(pmd.iter_months(start, end))

    # Merge with any existing CSV so partial runs don't erase earlier results
    existing: dict[str, dict] = {}
    if COVERAGE_CSV.exists():
        with COVERAGE_CSV.open(newline="") as f:
            existing = {r["month"]: r for r in csv.DictReader(f)}

    for i, m in enumerate(months, 1):
        status, url, html = fetch_month(m, offline=args.offline)
        row = {"month": m.strftime("%Y-%m"), "status": status, "url": url,
               "days_in_month": "", "tarbela_days": 0, "mangla_days": 0,
               "header_month_ok": "", "problems": ""}
        if html is not None:
            parsed = pmd.parse_month_html(html, m)
            row.update(
                days_in_month=parsed.days_in_month,
                tarbela_days=parsed.days_filled["tarbela"],
                mangla_days=parsed.days_filled["mangla"],
                header_month_ok="" if parsed.header_month is None else parsed.header_month == m,
                problems=" | ".join(parsed.problems),
            )
        elif args.offline and row["month"] in existing:
            continue  # keep the earlier online result
        existing[row["month"]] = row
        print(f"[{i:>3}/{len(months)}] {row['month']}  {status:<12} "
              f"T={row['tarbela_days']:>2} M={row['mangla_days']:>2} {row['problems'][:60]}",
              flush=True)

    INVENTORY_DIR.mkdir(parents=True, exist_ok=True)
    with COVERAGE_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for k in sorted(existing):
            w.writerow({f: existing[k].get(f, "") for f in FIELDS})
    print(f"\nWrote {COVERAGE_CSV}. Next: python -m pipeline.coverage_report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
