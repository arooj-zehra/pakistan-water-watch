"""Backfill daily Tarbela/Mangla figures from news outlets' daily water stories.

    python -m pipeline.probe_news      # first: lists candidate posts per outlet
    python -m pipeline.backfill_news   # downloads, parses, combines, loads
    python -m pipeline.backfill_news --outlets app,profit

Steps:
  1. For every outlet with a post list in data/inventory/news/<outlet>_posts.csv,
     download the posts not yet cached (100 per request through the outlet's
     WordPress API). Raw posts live in data/raw/<outlet>/<year>.jsonl and are
     committed, so nothing is downloaded twice.
  2. Parse each post with the press-release parser; articles without daily
     figures are skipped. The release date comes from the dateline or title
     ("ISLAMABAD, Mar 4 (APP)", "Report – April 3, 2025") when that falls up
     to 3 days before the post date, otherwise the post date.
  3. Combine outlets: one row per day and dam, the most complete story wins,
     ties go to the outlet listed first in sources/wordpress.py.
  4. Where two stories disagree on a day's inflow, log both to
     data/inventory/news_conflicts.csv (a free cross-check between outlets).
  5. Validate, upsert into `observations` (source = outlet key) and write
     data/inventory/news_parsed.csv with the article link for every number.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

from . import db
from .config import DATA_DIR, INVENTORY_DIR
from .sources import press_release as pr
from .sources import wordpress as wp
from .validate import db_lookups, validate_rows

NEWS_INVENTORY = INVENTORY_DIR / "news"
PARSED_CSV = INVENTORY_DIR / "news_parsed.csv"
CONFLICTS_CSV = INVENTORY_DIR / "news_conflicts.csv"
LEGACY_APP_LIST = INVENTORY_DIR / "app_releases.csv"  # written by probe_sources
FIELDS = ("level_ft", "inflow_cusecs", "outflow_cusecs", "live_storage_maf")
MAX_DATELINE_LAG_DAYS = 3
CONFLICT_TOLERANCE = 0.01  # 1% difference in inflow counts as a disagreement


def raw_dir(outlet: str):
    return DATA_DIR / "raw" / outlet


def posts_csv(outlet: str):
    return NEWS_INVENTORY / f"{outlet}_posts.csv"


# ---------------------------------------------------------------------------
# Raw cache (one JSONL file per outlet per year)
# ---------------------------------------------------------------------------
def load_cache(outlet: str) -> dict[int, dict]:
    posts = {}
    for path in sorted(raw_dir(outlet).glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    p = json.loads(line)
                    posts[int(p["id"])] = p
    return posts


def save_cache(outlet: str, posts: dict[int, dict]) -> None:
    d = raw_dir(outlet)
    d.mkdir(parents=True, exist_ok=True)
    by_year = defaultdict(list)
    for p in posts.values():
        by_year[p["date"][:4]].append(p)
    for year, items in by_year.items():
        with (d / f"{year}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
            for p in sorted(items, key=lambda p: (p["date"], p["id"])):
                f.write(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n")


def wanted_ids(outlet: str) -> list[int]:
    path = posts_csv(outlet)
    if not path.exists() and outlet == "app" and LEGACY_APP_LIST.exists():
        path = LEGACY_APP_LIST
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return sorted({int(r["id"]) for r in csv.DictReader(f) if r.get("id")})


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS |= {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS["sept"] = 9
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))
DATELINE_RE = re.compile(r"\b[A-Z][A-Za-z]{2,}\s*,\s*(" + _MON + r")\.?\s+(\d{1,2})\s*\(", re.I)
FULL_DATE_RE = re.compile(r"\b(" + _MON + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAY_RE = re.compile(r"\b(" + "|".join(WEEKDAYS) + r")\b", re.I)


def release_date(post: dict, text: str) -> date:
    """Date the figures refer to.

    In order: an explicit dateline ("ISLAMABAD, Mar 4 (APP)") or title date
    ("Report – April 3, 2025"); else the weekday named near the top ("IRSA
    Wednesday released ..."), taken as the most recent such day; else the post
    date. Each must fall 0-3 days before the post date. The weekday rule
    matters: Pakistan Observer reprints APP's story in the next morning's
    paper, so its post date is one day after the release it reports.
    """
    posted = date.fromisoformat(post["date"][:10])
    candidates = []
    if m := DATELINE_RE.search(text[:300]):
        mon, day = _MONTHS[m.group(1).lower()], int(m.group(2))
        year = posted.year - (1 if mon > posted.month else 0)  # Dec story posted in Jan
        candidates.append((year, mon, day))
    if m := FULL_DATE_RE.search(post.get("title", "")):
        candidates.append((int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))))
    for y, mo, d in candidates:
        try:
            cand = date(y, mo, d)
        except ValueError:
            continue
        if 0 <= (posted - cand).days <= MAX_DATELINE_LAG_DAYS:
            return cand
    if m := WEEKDAY_RE.search(text[:300]):
        lag = (posted.weekday() - WEEKDAYS.index(m.group(1).lower())) % 7
        if lag <= MAX_DATELINE_LAG_DAYS:
            return posted - timedelta(days=lag)
    return posted


# ---------------------------------------------------------------------------
# Parsing and combining
# ---------------------------------------------------------------------------
def parse_post(post: dict) -> pr.Release | None:
    """Parsed release, or None if the post isn't a daily figures story."""
    text = pr.html_to_text(post["content"]) if "<" in post["content"] else post["content"]
    try:
        rel = pr.parse_release(text, release_date(post, text))
    except (pr.ParseError, ValueError):
        return None
    if not any(s.inflow_cusecs is not None for s in rel.stations.values()):
        return None
    return rel


def _completeness(row: dict) -> int:
    return sum(row.get(f) is not None for f in FIELDS)


def _disagree(a, b) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) > CONFLICT_TOLERANCE * max(abs(a), abs(b), 1)


def best_rows(posts_by_outlet: dict[str, list[dict]]) -> tuple[list[dict], dict, list[dict]]:
    """Combine outlets. Returns (rows, stats, conflicts).

    posts_by_outlet must be ordered by outlet priority (dicts keep insertion order).
    stats: {outlet: Counter(releases=, not_a_release=)}.
    """
    stats = {o: Counter() for o in posts_by_outlet}
    candidates: dict[tuple, list[dict]] = defaultdict(list)
    for rank, (outlet, posts) in enumerate(posts_by_outlet.items()):
        for post in sorted(posts, key=lambda p: (p["date"], p["id"])):
            rel = parse_post(post)
            if rel is None:
                stats[outlet]["not_a_release"] += 1
                continue
            stats[outlet]["releases"] += 1
            for row in rel.observation_rows(source=outlet):
                if row["inflow_cusecs"] is None and row["level_ft"] is None:
                    continue
                row |= {"post_id": post["id"], "link": post["link"], "rank": rank,
                        "dead_level_ft": rel.stations[row["station_id"]].dead_level_ft}
                candidates[(row["date"], row["station_id"])].append(row)

    rows, conflicts = [], []
    for key, cands in candidates.items():
        best = max(cands, key=lambda r: (_completeness(r), -r["rank"], -r["post_id"]))
        rows.append(best)
        for other in cands:
            if other is not best and _disagree(best["inflow_cusecs"], other["inflow_cusecs"]):
                conflicts.append({"date": key[0], "station_id": key[1],
                                  "kept_source": best["source"], "kept_inflow": best["inflow_cusecs"],
                                  "kept_link": best["link"], "other_source": other["source"],
                                  "other_inflow": other["inflow_cusecs"], "other_link": other["link"]})
    rows.sort(key=lambda r: (r["station_id"], r["date"]))
    conflicts.sort(key=lambda c: (c["date"], c["station_id"]))
    return rows, stats, conflicts


def write_csv(path, rows: list[dict], cols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_rows(conn, rows: list[dict]) -> tuple[int, int]:
    dead, prev = db_lookups(conn)
    clean = [{k: r[k] for k in ("date", "station_id", *FIELDS, "source")} for r in rows]
    good, bad = validate_rows(clean, dead_level_lookup=dead, previous_level=prev)
    db.upsert(conn, db.observations, [{**g, "fetched_at": db.utcnow()} for g in good],
              keys=["date", "station_id"])
    db.add_quarantine(conn, bad, source="news")
    return len(good), len(bad)


def download_missing(outlet: str, ids: list[int], get) -> dict[int, dict]:
    cache = load_cache(outlet)
    missing = [i for i in ids if i not in cache]
    if missing:
        site = wp.OUTLET_BY_KEY[outlet].site
        endpoint = wp.find_endpoint(get, site)
        try:
            for p in wp.fetch(get, endpoint, missing,
                              progress=lambda n, t: print(f"     {outlet}: downloaded {n}/{t}", flush=True)):
                cache[p["id"]] = p
        finally:
            save_cache(outlet, cache)
    return cache


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outlets", help="comma-separated outlet keys (default: all with a post list)")
    args = ap.parse_args(argv)
    keys = args.outlets.split(",") if args.outlets else [o.key for o in wp.OUTLETS]
    keys = [k for k in (o.key for o in wp.OUTLETS) if k in keys and wanted_ids(k)]
    if not keys:
        print("No post lists found. Run: python -m pipeline.probe_news")
        return 1

    from . import http

    print("1) Downloading posts")
    posts_by_outlet = {}
    for k in keys:
        ids = wanted_ids(k)
        try:
            cache = download_missing(k, ids, http.get)
        except Exception as e:
            print(f"     {k}: download FAILED ({e}); using {len(load_cache(k))} cached posts")
            cache = load_cache(k)
        posts_by_outlet[k] = [cache[i] for i in ids if i in cache]
        print(f"     {k}: {len(posts_by_outlet[k])}/{len(ids)} posts available")

    rows, stats, conflicts = best_rows(posts_by_outlet)
    write_csv(PARSED_CSV, sorted(rows, key=lambda r: (r["date"], r["station_id"])),
              ["date", "station_id", *FIELDS, "dead_level_ft", "source", "post_id", "link"])
    write_csv(CONFLICTS_CSV, conflicts, ["date", "station_id", "kept_source", "kept_inflow", "kept_link",
                                         "other_source", "other_inflow", "other_link"])
    print("\n2) Parsed")
    used = Counter(r["source"] for r in rows)
    for k in keys:
        s = stats[k]
        print(f"     {k:<14} {s['releases']:>4} daily releases, {s['not_a_release']:>4} other articles; "
              f"chosen for {used[k]} dam-days")
    print(f"     {len(conflicts)} dam-days where outlets disagree on inflow by >1% "
          f"(see {CONFLICTS_CSV.name})")

    eng = db.get_engine()
    db.init_db(eng)
    with eng.begin() as conn:
        ok, bad = load_rows(conn, rows)
        db.log_run(conn, "news_backfill", "ok" if ok else "failed", ok,
                   f"{bad} quarantined" if bad else None)
    print(f"\n3) Loaded {ok} rows into observations, {bad} quarantined")

    per_year = defaultdict(Counter)
    for r in rows:
        if r["inflow_cusecs"] is not None:
            per_year[r["date"].year][r["station_id"]] += 1
    print("\n   Days with inflow, all outlets combined:   Tarbela  Mangla")
    for y in sorted(per_year):
        print(f"     {y}:                                    {per_year[y]['tarbela']:>5}   {per_year[y]['mangla']:>5}")
    print(f"\n   Wrote {PARSED_CSV.name}. Next: python -m pipeline.coverage_report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
