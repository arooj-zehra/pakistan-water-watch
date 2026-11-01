"""Write the website's data files, so it loads instantly even if the API sleeps.

    python -m pipeline.export_static              # into web/public/data/
    python -m pipeline.export_static --out some/dir

Writes latest.json, history.json, climatology.json and meta.json. These are
exactly what the API's /api/bundle/<name> endpoints return, so the website can
read either. The daily GitHub workflow runs this after ingestion and commits
the files; Vercel then redeploys the site with fresh data.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import db, queries
from .config import ROOT

DEFAULT_OUT = ROOT / "web" / "public" / "data"


def build(engine) -> dict[str, dict]:
    with engine.connect() as conn:
        bundles = {
            "latest": queries.latest(conn),
            "history": queries.history(conn),
            "climatology": queries.climatology(conn),
        }
    bundles["meta"] = queries.meta(engine)
    return bundles


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    eng = db.get_engine()
    db.init_db(eng)
    bundles = build(eng)
    if not bundles["latest"]["stations"]:
        print("The database has no observations yet. Run backfill_news first.")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    for name, payload in bundles.items():
        path = args.out / f"{name}.json"
        path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        print(f"  {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}  {path.stat().st_size // 1024} KB")
    for s in bundles["latest"]["stations"]:
        print(f"  {s['name']}: {s['date']}, level {s['level_ft']} ft, inflow {s['inflow_cusecs']} cusecs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
