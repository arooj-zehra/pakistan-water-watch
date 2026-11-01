"""Phase 1: the daily job (run by .github/workflows/daily_ingest.yml).

    python -m pipeline.daily                 # everything
    python -m pipeline.daily --skip-weather
    python -m pipeline.daily --press-file release.txt --date 2026-10-31
                                             # parse a saved release by hand
    python -m pipeline.daily --with-pmd      # also try PMD's monthly sheet

Steps, each logged to ingest_log:
  1. News: the last few days' IRSA/WAPDA stories from every outlet that has a
     post list (APP first), through their WordPress APIs. New posts are added
     to the raw cache and post lists, so the history keeps growing.
  2. WAPDA press release: if PRESS_RELEASE_URL is set (or --press-file given)
  3. Weather: last 14 days of ERA5 + today's 16-day forecast for every point
  4. Freshness check: newest inflow must be at most 3 days old
  PMD's monthly sheets stopped in 2015, so that step only runs with --with-pmd.

Exit code 1 means a parser broke or data is stale; the workflow then opens a
GitHub issue so a format change never goes unnoticed.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, timedelta

from . import db
from .config import PRESS_RELEASE_URL, RAW_PMD_DIR, RAW_PRESS_DIR, WEATHER_POINTS
from .validate import db_lookups, validate_rows

STALE_AFTER_DAYS = 3


def step_pmd(conn, today: date) -> tuple[str, int, str | None]:
    from .probe_pmd import fetch_month
    from .backfill import load_pmd_files
    from .sources import pmd

    months = [today.replace(day=1)]
    if today.day <= 3:
        months.append((today.replace(day=1) - timedelta(days=1)).replace(day=1))
    paths, errors = [], []
    for m in months:
        status, url, html = fetch_month(m, refresh_recent=True)
        if html is None:
            errors.append(f"{m:%b %Y}: {status}")
        else:
            paths.append(pmd.raw_path(m))
    if not paths:
        return "failed", 0, "; ".join(errors)
    ok, bad = load_pmd_files(conn, paths)
    return ("partial" if errors or bad else "ok"), ok, ("; ".join(errors) or None)


NEWS_LOOKBACK_DAYS = 4


def daily_outlets() -> list[str]:
    """Outlets with a post list (i.e. that probe_news found usable), APP first."""
    from .backfill_news import wanted_ids
    from .sources import wordpress as wp

    keys = [o.key for o in wp.OUTLETS if wanted_ids(o.key)]
    return keys or ["app"]


def _append_post_list(outlet: str, posts: list[dict]) -> None:
    import csv

    from .backfill_news import posts_csv

    path = posts_csv(outlet)
    known = set()
    if path.exists():
        with path.open(encoding="utf-8") as f:
            known = {r["id"] for r in csv.DictReader(f)}
    new = [p for p in posts if str(p["id"]) not in known]
    if not new:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "date", "title", "link"], extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerows(new)


def step_news(conn, today: date, get=None) -> tuple[str, int, str | None]:
    from . import backfill_news as bn
    from .sources import wordpress as wp

    if get is None:
        from . import http

        get = http.get
    after = (today - timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat() + "T00:00:00"
    recent, errors = {}, []
    for key in daily_outlets():
        try:
            endpoint = wp.find_endpoint(get, wp.OUTLET_BY_KEY[key].site)
            listed = {}
            for q in wp.SEARCH_QUERIES:
                for p in wp.search(get, endpoint, q, after=after, max_pages=2):
                    listed.setdefault(p["id"], p)
            cache = bn.load_cache(key)
            missing = [i for i in listed if i not in cache]
            if missing:
                for p in wp.fetch(get, endpoint, missing):
                    cache[p["id"]] = p
                bn.save_cache(key, cache)
            _append_post_list(key, list(listed.values()))
            recent[key] = [cache[i] for i in listed if i in cache]
        except Exception as e:
            errors.append(f"{key}: {e}")
    rows, _, conflicts = bn.best_rows(recent)
    ok, bad = bn.load_rows(conn, rows) if rows else (0, 0)
    notes = errors + ([f"{bad} quarantined"] if bad else []) + \
        ([f"{len(conflicts)} outlet disagreements"] if conflicts else [])
    status = "ok" if ok and not errors and not bad else ("partial" if ok else "failed")
    return status, ok, "; ".join(notes) or None


def step_press(conn, today: date, *, url: str | None, text_file: str | None) -> tuple[str, int, str | None]:
    from .sources import press_release as pr

    if text_file:
        with open(text_file, encoding="utf-8") as f:
            raw = f.read()
        text = pr.html_to_text(raw) if "<html" in raw[:2000].lower() else raw
    else:
        from . import http

        r = http.get(url)
        r.raise_for_status()
        raw = r.text
        RAW_PRESS_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_PRESS_DIR / f"{today.isoformat()}.html").write_text(raw, encoding="utf-8")
        text = pr.html_to_text(raw)

    try:
        rel = pr.parse_release(text, today)
    except pr.ParseError as e:
        db.add_quarantine(conn, [(text[:5000], str(e))], source="wapda_press")
        return "failed", 0, str(e)

    dead, prev = db_lookups(conn)
    good, bad = validate_rows(rel.observation_rows(), dead_level_lookup=dead, previous_level=prev)
    db.upsert(conn, db.observations, [{**g, "fetched_at": db.utcnow()} for g in good],
              keys=["date", "station_id"])
    db.add_quarantine(conn, bad, source="wapda_press")

    # Record a new dead level when WAPDA's reported value changes
    for sid, s in rel.stations.items():
        if s.dead_level_ft is not None and db.dead_level_on(conn, sid, today) != s.dead_level_ft:
            db.upsert(conn, db.dead_levels, [{"station_id": sid, "valid_from": today,
                                             "level_ft": s.dead_level_ft, "source": "wapda_press"}],
                      keys=["station_id", "valid_from"])
    status = "ok" if good and not bad and not rel.missing else ("partial" if good else "failed")
    notes = ([f"quarantined: {reason}" for _, reason in bad]
             + (["missing: " + ", ".join(rel.missing)] if rel.missing else []))
    return status, len(good), "; ".join(notes) or None


def step_weather(conn, today: date) -> tuple[str, int, str | None]:
    from .sources import openmeteo

    n, errors = 0, []
    for p in WEATHER_POINTS:
        try:
            era = openmeteo.fetch_archive(p, today - timedelta(days=14), today - timedelta(days=1))
            # ERA5's newest days come back as nulls until processed: skip them
            era = [{k: v for k, v in r.items() if k not in ("kind", "model")} | {"fetched_at": db.utcnow()}
                   for r in era if r["tmean_c"] is not None]
            n += db.upsert(conn, db.weather, era, keys=["date", "point_id"])
            fc = [{k: v for k, v in r.items() if k not in ("kind", "model")} | {"issued_on": today, "fetched_at": db.utcnow()}
                  for r in openmeteo.fetch_forecast(p)]
            n += db.upsert(conn, db.weather_forecast, fc, keys=["issued_on", "date", "point_id"])
        except Exception as e:
            errors.append(f"{p.id}: {e}")
    status = "ok" if not errors else ("partial" if n else "failed")
    return status, n, "; ".join(errors) or None


def run(today: date | None = None, *, skip_weather=False, with_pmd=False, skip_news=False,
        press_url: str | None = PRESS_RELEASE_URL, press_file: str | None = None) -> int:
    today = today or date.today()
    eng = db.get_engine()
    db.init_db(eng)
    broken = []

    steps = []
    if not skip_news:
        steps.append(("news", lambda c: step_news(c, today)))
    if with_pmd:
        steps.append(("pmd", lambda c: step_pmd(c, today)))
    if press_url or press_file:
        steps.append(("wapda_press", lambda c: step_press(c, today, url=press_url, text_file=press_file)))
    if not skip_weather:
        steps.append(("weather", lambda c: step_weather(c, today)))

    for name, fn in steps:
        # Each step in its own transaction: one failing source doesn't lose the others
        with eng.begin() as conn:
            try:
                status, n, err = fn(conn)
            except Exception as e:
                status, n, err = "failed", 0, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
            db.log_run(conn, name, status, n, err)
        print(f"{name:<12} {status:<8} rows={n:<5} {(err or '')[:200]}")
        if status == "failed" and name != "weather":
            broken.append(name)

    with eng.begin() as conn:
        for sid in ("tarbela", "mangla"):
            last = db.latest_observation_date(conn, sid)
            if last is None or (today - last).days > STALE_AFTER_DAYS:
                msg = f"{sid}: newest inflow is {last or 'missing'} (> {STALE_AFTER_DAYS} days old)"
                db.log_run(conn, "freshness", "stale", 0, msg)
                print(f"freshness    stale    {msg}")
                broken.append(f"stale:{sid}")
    return 1 if broken else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="override today (YYYY-MM-DD)")
    ap.add_argument("--skip-weather", action="store_true")
    ap.add_argument("--with-pmd", action="store_true", help="also try PMD (archive stopped in 2015)")
    ap.add_argument("--skip-news", action="store_true")
    ap.add_argument("--press-file", help="parse a saved release (text or HTML)")
    args = ap.parse_args(argv)
    today = date.fromisoformat(args.date) if args.date else None
    return run(today, skip_weather=args.skip_weather, with_pmd=args.with_pmd,
               skip_news=args.skip_news, press_file=args.press_file)


if __name__ == "__main__":
    sys.exit(main())
