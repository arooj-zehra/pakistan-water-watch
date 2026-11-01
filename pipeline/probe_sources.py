"""Phase 0, task 2 (fallback): where else can daily history come from?

The PMD archive only has Sep-Dec 2015, so this checks two other routes and
reports how much each could give us. Nothing is scraped in bulk here; it only
counts what exists.

  1. Internet Archive (Wayback Machine): were PMD's monthly sheets for other
     years ever captured before they disappeared from pmd.gov.pk?
  2. APP (Associated Press of Pakistan, a WordPress site): its search API
     lists the daily "IRSA releases ... cusecs" and "WAPDA issues water
     position" stories, which carry Tarbela/Mangla inflow, outflow and level.

    python -m pipeline.probe_sources

Writes data/inventory/wayback_pmd.csv and data/inventory/app_releases.csv.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import Counter

from .config import INVENTORY_DIR

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
APP_API = "https://www.app.com.pk/wp-json/wp/v2/posts"
# WordPress search ANDs the words across title and body. Headlines vary a lot
# ("IRSA releases ...", "Water position in rivers ...", "Rim stations ..."),
# but every daily release names a dam and "cusecs", so those queries catch
# stories the headline phrases miss. Non-release articles are filtered out
# later by the parser.
APP_QUERIES = [
    "IRSA releases cusecs",
    "WAPDA issues water position",
    "Tarbela cusecs",
    "Mangla cusecs",
    "rim stations cusecs",
]
WAYBACK_RETRY_WAITS_S = (20, 60, 120)
MONTH_RE = re.compile(r"dams([a-z]{3})(\d{2})", re.I)


def wayback_pmd(get, sleep=None) -> list[dict]:
    """Every archived URL under PMD's FFD daily folder, one row per capture.

    The CDX server is often overloaded (HTTP 503), so retry with pauses."""
    import time

    sleep = sleep or time.sleep
    params = {"url": "pmd.gov.pk/FFD/index_files/daily/", "matchType": "prefix",
              "output": "json", "fl": "original,timestamp,statuscode,mimetype", "limit": "20000"}
    for attempt in range(len(WAYBACK_RETRY_WAITS_S) + 1):
        r = get(WAYBACK_CDX, delay=1.0, timeout=120, params=params)
        if r.status_code in (429, 502, 503, 504) and attempt < len(WAYBACK_RETRY_WAITS_S):
            wait = WAYBACK_RETRY_WAITS_S[attempt]
            print(f"   Internet Archive busy (HTTP {r.status_code}); retrying in {wait}s...", flush=True)
            sleep(wait)
            continue
        break
    r.raise_for_status()
    data = r.json()
    if not data:
        return []
    header, rows = data[0], data[1:]
    return [dict(zip(header, row)) for row in rows]


def summarise_wayback(captures: list[dict]) -> tuple[list[str], Counter]:
    ok = [c for c in captures if c.get("statuscode") == "200"]
    months = sorted({f"{m.group(1).lower()}{m.group(2)}" for c in ok
                     if (m := MONTH_RE.search(c["original"])) and "sheet" in c["original"].lower()})
    per_year = Counter("20" + m[-2:] for m in months)
    return months, per_year


def app_search(get, query: str, max_pages: int = 30) -> tuple[list[dict], str]:
    """List matching APP posts (date, title, link). Returns (posts, status)."""
    posts = []
    for page in range(1, max_pages + 1):
        r = get(APP_API, delay=2.0, timeout=60, params={
            "search": query, "per_page": 100, "page": page,
            "_fields": "id,date,link,title", "orderby": "date", "order": "desc",
        })
        if r.status_code == 400 and page > 1:  # past the last page
            break
        if r.status_code != 200:
            return posts, f"HTTP {r.status_code}"
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        for p in batch:
            title = p.get("title", {})
            posts.append({"id": p.get("id"), "date": p.get("date", "")[:10], "link": p.get("link"),
                          "title": re.sub(r"<[^>]+>", "", title.get("rendered", "") if isinstance(title, dict) else str(title)),
                          "query": query})
        total_pages = int(r.headers.get("X-WP-TotalPages", page)) if hasattr(r, "headers") else page
        if page >= total_pages:
            break
    return posts, "ok"


def dedupe_posts(posts: list[dict]) -> list[dict]:
    """One row per post id; `query` lists every query that found it."""
    by_id: dict = {}
    for p in posts:
        if p["id"] in by_id:
            q = by_id[p["id"]]["query"]
            if p["query"] not in q.split(" | "):
                by_id[p["id"]]["query"] = q + " | " + p["query"]
        else:
            by_id[p["id"]] = dict(p)
    return sorted(by_id.values(), key=lambda p: (p["date"], str(p["id"])))


def days_per_year(posts: list[dict]) -> list[tuple[str, int, int]]:
    """(year, days with a post, days in that year so far)."""
    import calendar
    from datetime import date

    today = date.today()
    days = {p["date"] for p in posts if p["date"]}
    out = []
    for y in sorted({d[:4] for d in days}):
        yi = int(y)
        total = (today - date(yi, 1, 1)).days + 1 if yi == today.year else (366 if calendar.isleap(yi) else 365)
        out.append((y, sum(1 for d in days if d.startswith(y)), total))
    return out


def main() -> int:
    from . import http

    INVENTORY_DIR.mkdir(parents=True, exist_ok=True)
    exit_code = 0

    print("1) Internet Archive: archived PMD monthly sheets")
    try:
        caps = wayback_pmd(http.get)
        months, per_year = summarise_wayback(caps)
        with (INVENTORY_DIR / "wayback_pmd.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["original", "timestamp", "statuscode", "mimetype"])
            w.writeheader()
            w.writerows(caps)
        print(f"   {len(caps)} captures; {len(months)} distinct monthly sheets with HTTP 200")
        for y in sorted(per_year):
            print(f"   {y}: {per_year[y]:>2} month(s)")
    except Exception as e:
        print(f"   FAILED: {type(e).__name__}: {e}")
        exit_code = 1

    print("\n2) APP daily water stories (WordPress search API)")
    all_posts = []
    for q in APP_QUERIES:
        try:
            posts, status = app_search(http.get, q)
        except Exception as e:
            posts, status = [], f"{type(e).__name__}: {e}"
        dates = sorted(p["date"] for p in posts if p["date"])
        span = f"{dates[0]} to {dates[-1]}" if dates else "none"
        print(f"   '{q}': {status}, {len(posts)} posts, {span}")
        if dates:
            per_year = Counter(d[:4] for d in dates)
            print("   per year: " + ", ".join(f"{y}={n}" for y, n in sorted(per_year.items())))
        all_posts += posts
        if status != "ok":
            exit_code = 1
    unique = dedupe_posts(all_posts)
    with (INVENTORY_DIR / "app_releases.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "date", "title", "link", "query"])
        w.writeheader()
        w.writerows(unique)
    print(f"\n   Combined: {len(unique)} distinct posts on {len({p['date'] for p in unique})} distinct days")
    print("   Days with at least one candidate story, per year (before parsing):")
    for y, n, total in days_per_year(unique):
        print(f"     {y}: {n:>3}/{total} days ({n / total:.0%})")
    print("\n   Next: python -m pipeline.backfill_app  (downloads and parses these stories)")
    print("\nPaste this whole output back into the chat.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
