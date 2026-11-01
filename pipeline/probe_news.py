"""Which news outlets can fill the days APP misses?

    python -m pipeline.probe_news                 # every outlet in sources/wordpress.py
    python -m pipeline.probe_news --outlets profit,dailytimes

For each outlet: check it has a usable WordPress API, list every post matching
"Tarbela cusecs" / "Mangla cusecs", and save the list to
data/inventory/news/<outlet>_posts.csv (backfill_news downloads from these).
Then count, per year, how many days each outlet adds on top of APP.

These are candidate posts: some will turn out not to be daily releases once
parsed (flood news also mentions Tarbela and cusecs), so treat the counts as an
upper bound. backfill_news + coverage_report give the real numbers.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict

from .config import INVENTORY_DIR
from .sources import wordpress as wp

NEWS_INVENTORY = INVENTORY_DIR / "news"
LEGACY_APP_LIST = INVENTORY_DIR / "app_releases.csv"


def probe_outlet(get, outlet: wp.Outlet) -> tuple[list[dict], str]:
    """(deduplicated posts, status)."""
    try:
        endpoint = wp.find_endpoint(get, outlet.site)
    except wp.WordPressError as e:
        return [], str(e)
    by_id = {}
    for q in wp.SEARCH_QUERIES:
        try:
            for p in wp.search(get, endpoint, q):
                by_id.setdefault(p["id"], p)
        except wp.WordPressError as e:
            return list(by_id.values()), f"search failed: {e}"
    return sorted(by_id.values(), key=lambda p: (p["date"], p["id"])), "ok"


def save_posts(outlet_key: str, posts: list[dict]) -> None:
    NEWS_INVENTORY.mkdir(parents=True, exist_ok=True)
    with (NEWS_INVENTORY / f"{outlet_key}_posts.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "date", "title", "link"], extrasaction="ignore")
        w.writeheader()
        w.writerows(posts)


def load_posts(outlet_key: str) -> list[dict]:
    path = NEWS_INVENTORY / f"{outlet_key}_posts.csv"
    if not path.exists() and outlet_key == "app" and LEGACY_APP_LIST.exists():
        path = LEGACY_APP_LIST
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def days_by_year(posts: list[dict]) -> dict[str, set[str]]:
    out = defaultdict(set)
    for p in posts:
        d = (p.get("date") or "")[:10]
        if d:
            out[d[:4]].add(d)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outlets", help="comma-separated outlet keys (default: all)")
    args = ap.parse_args(argv)
    chosen = [o for o in wp.OUTLETS if not args.outlets or o.key in args.outlets.split(",")]

    from . import http

    print("1) Checking outlets (this can take a while: 2 s between requests)")
    found: dict[str, list[dict]] = {}
    for o in chosen:
        posts, status = probe_outlet(http.get, o)
        if posts:
            save_posts(o.key, posts)
        span = f"{posts[0]['date'][:10]} to {posts[-1]['date'][:10]}" if posts else ""
        print(f"   {o.key:<14} {status:<40.40} {len(posts):>5} posts {span}", flush=True)
        if posts:
            found[o.key] = posts

    app_days = days_by_year(found.get("app") or load_posts("app"))
    years = sorted({y for posts in found.values() for y in days_by_year(posts)} | set(app_days))
    years = [y for y in years if y >= "2020"]
    print("\n2) Candidate days per year: APP alone, extra days each outlet adds, all combined")
    header = "   outlet         " + "".join(f"{y:>7}" for y in years)
    print(header)
    print("   app            " + "".join(f"{len(app_days.get(y, ())):>7}" for y in years))
    combined = {y: set(app_days.get(y, ())) for y in years}
    for k, posts in found.items():
        if k == "app":
            continue
        d = days_by_year(posts)
        extra = {y: d.get(y, set()) - app_days.get(y, set()) for y in years}
        for y in years:
            combined[y] |= d.get(y, set())
        print(f"   +{k:<13} " + "".join(f"{len(extra[y]):>7}" for y in years))
    print("   = combined     " + "".join(f"{len(combined[y]):>7}" for y in years))
    print("\n   Next: python -m pipeline.backfill_news   (downloads and parses all of these)")
    print("\nPaste this output back into the chat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
