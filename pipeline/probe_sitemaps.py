"""Which non-WordPress sites can fill the remaining gaps? (via their sitemaps)

    python -m pipeline.probe_sitemaps                     # every site in sources/sitemaps.py
    python -m pipeline.probe_sitemaps --sites brecorder,urdupoint
    python -m pipeline.probe_sitemaps --max-sitemaps 300  # cap per site (default 600)

For each site: read robots.txt, walk its sitemaps, and keep article URLs whose
slug or title looks like a daily water report. Saves
data/inventory/html/<site>_candidates.csv (backfill_html downloads from these)
and counts, per year, candidate days that we don't already have.

Candidate dates come from the sitemap's lastmod (or a date in the URL), so
they are approximate until the articles are downloaded and parsed.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict

from .config import INVENTORY_DIR
from .sources import sitemaps as sm

HTML_INVENTORY = INVENTORY_DIR / "html"
NEWS_PARSED = INVENTORY_DIR / "news_parsed.csv"
_URL_DATE = re.compile(r"/(20\d\d)[/-](\d\d)[/-](\d\d)/")


def candidate_day(entry: dict) -> str:
    if m := _URL_DATE.search(entry["loc"]):
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return (entry.get("lastmod") or "")[:10]


def known_days() -> set[str]:
    """Days where we already have inflow for both dams."""
    if not NEWS_PARSED.exists():
        return set()
    per_day = defaultdict(set)
    with NEWS_PARSED.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("inflow_cusecs"):
                per_day[r["date"]].add(r["station_id"])
    return {d for d, s in per_day.items() if len(s) == 2}


def save(site_key: str, entries: list[dict]) -> None:
    HTML_INVENTORY.mkdir(parents=True, exist_ok=True)
    with (HTML_INVENTORY / f"{site_key}_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["loc", "lastmod", "title"], extrasaction="ignore")
        w.writeheader()
        w.writerows(entries)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sites", help="comma-separated site keys (default: all)")
    ap.add_argument("--max-sitemaps", type=int, default=600)
    args = ap.parse_args(argv)
    chosen = [s for s in sm.SITES if not args.sites or s.key in args.sites.split(",")]

    from . import http

    have = known_days()
    print(f"Days already covered for both dams: {len(have)}")
    print("1) Walking sitemaps (2 s between requests; big sites take several minutes)")
    results = {}
    for s in chosen:
        entries, status = sm.walk(http.get, s, max_sitemaps=args.max_sitemaps)
        if entries:
            save(s.key, entries)
            results[s.key] = entries
        print(f"   {s.key:<14} {status:<45.45} {len(entries):>5} candidate articles", flush=True)

    years = [str(y) for y in range(2020, 2027)]
    print("\n2) Candidate days per year that we DON'T already have (upper bound until parsed)")
    print("   site           " + "".join(f"{y:>7}" for y in years))
    union = defaultdict(set)
    for k, entries in results.items():
        new = defaultdict(set)
        for e in entries:
            d = candidate_day(e)
            if d and d not in have:
                new[d[:4]].add(d)
                union[d[:4]].add(d)
        print(f"   {k:<14} " + "".join(f"{len(new[y]):>7}" for y in years))
    print("   = all sites    " + "".join(f"{len(union[y]):>7}" for y in years))
    print("\n   Next: python -m pipeline.backfill_html   (downloads the candidate articles)")
    print("\nPaste this output back into the chat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
