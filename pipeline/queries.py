"""Read-side queries shared by the API and the static JSON export.

Both return exactly the same payloads, so the website can read either one:
the static files first (instant, survive a sleeping server), the API second.
Pure Python (no pandas) except `meta()`, which reuses the coverage report.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from statistics import median

from sqlalchemy import func, select

from . import db
from .config import STATIONS

FIELDS = ("level_ft", "inflow_cusecs", "outflow_cusecs", "live_storage_maf")
SOURCE_NAMES = {
    "app": "Associated Press of Pakistan (APP)",
    "urdupoint": "UrduPoint",
    "pakobserver": "Pakistan Observer",
    "customstoday": "Customs Today",
    "pmd": "PMD Flood Forecasting Division",
    "dailytimes": "Daily Times",
    "tribune": "Express Tribune",
    "profit": "Profit (Pakistan Today)",
    "pakistantoday": "Pakistan Today",
    "energyupdate": "Energy Update",
    "dailyausaf": "Daily Ausaf",
    "nation": "The Nation",
    "wapda_press": "WAPDA press release",
}
CLIMATOLOGY_WINDOW_DAYS = 7   # pool +/- 7 days across years: gaps are common
CLIMATOLOGY_MIN_VALUES = 5


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _rows(conn, station: str | None = None, start: date | None = None, end: date | None = None):
    o = db.observations.c
    q = select(o.date, o.station_id, o.level_ft, o.inflow_cusecs, o.outflow_cusecs,
               o.live_storage_maf, o.source).order_by(o.station_id, o.date)
    if station:
        q = q.where(o.station_id == station)
    if start:
        q = q.where(o.date >= start)
    if end:
        q = q.where(o.date <= end)
    rows = [dict(r) for r in conn.execute(q).mappings().all()]
    return apply_review(rows, station, start, end)


def apply_review(rows: list[dict], station=None, start=None, end=None) -> list[dict]:
    """Apply the decisions in data/review/decisions.csv (Phase 5) to rows read
    from the database, so every reader sees the same corrected history."""
    from . import anomaly

    dec = [d for d in anomaly.read_decisions()
           if (not station or d.get("station_id") == station)
           and (not start or d.get("date", "") >= start.isoformat())
           and (not end or d.get("date", "") <= end.isoformat())]
    return anomaly.apply_corrections(rows, dec) if dec else rows


def latest(conn, today: date | None = None) -> dict:
    """Newest figures per dam, with the previous day for change arrows."""
    today = today or date.today()
    o = db.observations.c
    out = []
    for sid, st in STATIONS.items():
        rows = [dict(r, station_id=sid) for r in conn.execute(
            select(o.date, o.level_ft, o.inflow_cusecs, o.outflow_cusecs, o.live_storage_maf, o.source)
            .where(o.station_id == sid, (o.inflow_cusecs.is_not(None)) | (o.level_ft.is_not(None)))
            .order_by(o.date.desc()).limit(10)).mappings().all()]
        rows = apply_review(rows, sid, rows[-1]["date"] if rows else None)
        recent = sorted((r for r in rows if r["inflow_cusecs"] is not None or r["level_ft"] is not None),
                        key=lambda r: r["date"], reverse=True)[:2]
        if not recent:
            continue
        cur = recent[0]
        prev = recent[1] if len(recent) > 1 else None
        dead = db.dead_level_on(conn, sid, cur["date"])
        out.append({
            "id": sid, "name": st.name, "river": st.river,
            "date": cur["date"].isoformat(),
            **{f: cur[f] for f in FIELDS},
            "source": cur["source"], "source_name": SOURCE_NAMES.get(cur["source"], cur["source"]),
            "previous": ({"date": prev["date"].isoformat(), **{f: prev[f] for f in FIELDS}} if prev else None),
            "dead_level_ft": dead, "max_level_ft": st.max_level_ft,
            "days_old": (today - cur["date"]).days,
        })
    return {"generated_at": _now(), "stations": out}


def history(conn, station: str | None = None, start: date | None = None, end: date | None = None) -> dict:
    """Daily series as column arrays per dam (compact for the website)."""
    series: dict[str, dict] = {}
    for r in _rows(conn, station, start, end):
        s = series.setdefault(r["station_id"], {"date": [], **{f: [] for f in FIELDS}, "source": []})
        s["date"].append(r["date"].isoformat())
        for f in FIELDS:
            s[f].append(r[f])
        s["source"].append(r["source"])
    return {"generated_at": _now(), "stations": series}


def history_rows(conn, station: str, start: date | None = None, end: date | None = None) -> list[dict]:
    """The same data as a list of records (friendlier for API users)."""
    return [{"date": r["date"].isoformat(), **{f: r[f] for f in FIELDS}, "source": r["source"]}
            for r in _rows(conn, station, start, end)]


def _pct(sorted_vals: list[float], p: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def climatology(conn, station: str | None = None) -> dict:
    """Per day of year (1-366): 10th, 50th and 90th percentile of inflow and level.

    Values within +/- CLIMATOLOGY_WINDOW_DAYS days are pooled across years,
    because the news-sourced history has gaps; a day needs at least
    CLIMATOLOGY_MIN_VALUES values or it is left empty (null).
    """
    out = {}
    stations = [station] if station else list(STATIONS)
    for sid in stations:
        by_doy = {"inflow_cusecs": {}, "level_ft": {}}
        years = set()
        for r in _rows(conn, sid):
            doy = r["date"].timetuple().tm_yday
            years.add(r["date"].year)
            for f in by_doy:
                if r[f] is not None:
                    by_doy[f].setdefault(doy, []).append(r[f])
        res = {"doy": list(range(1, 367)), "years": sorted(years)}
        for f, d in by_doy.items():
            p10, p50, p90, n = [], [], [], []
            for doy in range(1, 367):
                vals = []
                for k in range(-CLIMATOLOGY_WINDOW_DAYS, CLIMATOLOGY_WINDOW_DAYS + 1):
                    vals += d.get((doy + k - 1) % 366 + 1, [])
                vals.sort()
                ok = len(vals) >= CLIMATOLOGY_MIN_VALUES
                p10.append(round(_pct(vals, 0.1), 2) if ok else None)
                p50.append(round(median(vals), 2) if ok else None)
                p90.append(round(_pct(vals, 0.9), 2) if ok else None)
                n.append(len(vals))
            res[f] = {"p10": p10, "p50": p50, "p90": p90, "n": n}
        out[sid] = res
    return {"generated_at": _now(), "window_days": CLIMATOLOGY_WINDOW_DAYS, "stations": out}


def sources(conn) -> list[dict]:
    o = db.observations.c
    rows = conn.execute(
        select(o.source, func.count(), func.min(o.date), func.max(o.date))
        .where(o.inflow_cusecs.is_not(None)).group_by(o.source).order_by(func.count().desc())).all()
    return [{"source": s, "name": SOURCE_NAMES.get(s, s), "dam_days": n,
             "first": first.isoformat() if hasattr(first, "isoformat") else str(first),
             "last": last.isoformat() if hasattr(last, "isoformat") else str(last)} for s, n, first, last in rows]


def meta(engine) -> dict:
    """Sources, coverage per year (as recorded and after filling short gaps)."""
    from . import coverage_report as cr

    with engine.connect() as conn:
        src = sources(conn)
        newest = conn.execute(select(func.max(db.observations.c.date))).scalar()
    coverage = []
    daily = cr.db_daily(engine)
    if not daily.empty:
        raw_y = cr.yearly(cr.db_monthly(daily, fill=False))
        fill_y = cr.yearly(cr.db_monthly(daily, fill=True))
        for (yr, r), (_, f) in zip(raw_y.iterrows(), fill_y.iterrows()):
            coverage.append({"year": int(yr), "partial": bool(r["months_probed"] < 12),
                             "present_raw": round(100 * (1 - float(r["worst_missing"]))),
                             "present_filled": round(100 * (1 - float(f["worst_missing"])))})
    return {"generated_at": _now(), "latest_date": newest.isoformat() if newest else None,
            "fill_gap_days": cr.MAX_FILL_GAP_DAYS, "sources": src, "coverage": coverage}
