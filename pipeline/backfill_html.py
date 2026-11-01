"""Download the candidate articles that probe_sitemaps found, into the news cache.

    python -m pipeline.probe_sitemaps   # first: data/inventory/html/<site>_candidates.csv
    python -m pipeline.backfill_html    # downloads article text (2 s per page)
    python -m pipeline.backfill_news    # then: parses and merges with every other source

Only articles from 2017 on, on days we don't already have, are fetched, newest
first, so stopping early (Ctrl+C) still keeps the most useful ones. Each page
is fetched once (respecting robots.txt), reduced to its article text,
title and published date, and stored in data/raw/<site>/<year>.jsonl in the
same format as the WordPress outlets, so backfill_news treats all sources alike
(same parser, same dating rules, same disagreement log). Only the article text
is kept, not the whole page, so the repo stays small.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys

from bs4 import BeautifulSoup

from .backfill_news import load_cache, save_cache
from .probe_sitemaps import HTML_INVENTORY, candidate_day, known_days
from .sources import sitemaps as sm
from .sources.press_release import html_to_text

MAX_TEXT_CHARS = 8000
MIN_DAY = "2017-01-01"


def post_id(url: str) -> int:
    """Stable numeric id for a URL (the cache is keyed by integer ids)."""
    return int(hashlib.sha1(url.encode("utf-8")).hexdigest()[:12], 16)


def candidates(site_key: str) -> list[dict]:
    path = HTML_INVENTORY / f"{site_key}_candidates.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def select_todo(entries: list[dict], cache: dict, have: set[str], *, include_covered: bool = False) -> list[dict]:
    """Candidates worth downloading, newest first.

    Re-applies the (tightened) report filter, so lists made by an older probe
    don't need re-probing; drops pre-2017 articles, ones already cached, and,
    unless include_covered, ones on days we already have for both dams.
    """
    out = []
    for e in entries:
        day = candidate_day(e)
        if not day or day < MIN_DAY or post_id(e["loc"]) in cache or not sm.looks_like_report(e):
            continue
        if not include_covered and day in have:
            continue
        out.append(e)
    return sorted(out, key=lambda e: (candidate_day(e), e["loc"]), reverse=True)


def published_date(soup: BeautifulSoup) -> str:
    """ISO date-time from the usual places, or ''."""
    for attrs in ({"property": "article:published_time"}, {"property": "og:published_time"},
                  {"name": "publish-date"}, {"name": "pubdate"}, {"itemprop": "datePublished"},
                  {"name": "date"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return tag["content"].strip()
    for script in soup.find_all("script", type="application/ld+json"):
        if m := re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string or ""):
            return m.group(1)
    t = soup.find("time", attrs={"datetime": True})
    return t["datetime"].strip() if t else ""


def page_to_post(url: str, html: str | bytes, entry: dict) -> dict:
    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", attrs={"property": "og:title"})
    h1 = soup.find("h1")
    title = (og.get("content") if og and og.get("content") else
             h1.get_text(" ", strip=True) if h1 else
             soup.title.get_text(" ", strip=True) if soup.title else entry.get("title", ""))
    date = published_date(soup) or candidate_day(entry)
    text = html_to_text(html)[:MAX_TEXT_CHARS]
    return {"id": post_id(url), "date": date[:19] if len(date) >= 10 else "",
            "link": url, "title": title, "content": text.replace("<", " ")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sites", help="comma-separated site keys (default: all with candidates)")
    ap.add_argument("--max-pages", type=int, default=3000, help="per site, per run")
    ap.add_argument("--include-covered", action="store_true",
                    help="also fetch articles on days we already have (for cross-checking)")
    args = ap.parse_args(argv)
    keys = [s.key for s in sm.SITES if (not args.sites or s.key in args.sites.split(",")) and candidates(s.key)]
    if not keys:
        print("No candidate lists found. Run: python -m pipeline.probe_sitemaps")
        return 1

    from . import http

    have = known_days()
    for key in keys:
        site = sm.SITE_BY_KEY[key]
        robots = sm.Robots(http.get, site.site)
        cache = load_cache(key)
        todo = select_todo(candidates(key), cache, have, include_covered=args.include_covered)[: args.max_pages]
        print(f"{key}: {len(cache)} cached, {len(todo)} to download (about {len(todo) * 2 // 60 + 1} min)")
        failed = skipped = 0
        try:
            for i, e in enumerate(todo, 1):
                if not robots.allowed(e["loc"]):
                    skipped += 1
                    continue
                try:
                    r = http.get(e["loc"], delay=2.0, timeout=60)
                except Exception:
                    failed += 1
                    continue
                if r.status_code != 200:
                    failed += 1
                    continue
                p = page_to_post(e["loc"], r.content, e)
                if p["date"]:
                    cache[p["id"]] = p
                if i % 25 == 0:
                    print(f"   {key}: {i}/{len(todo)}", flush=True)
                    save_cache(key, cache)  # checkpoint, so an interruption loses little
        finally:
            save_cache(key, cache)
        print(f"   {key}: done, {len(cache)} cached ({failed} failed, {skipped} disallowed by robots.txt)")
    print("\nNext: python -m pipeline.backfill_news   (parses and merges everything)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
