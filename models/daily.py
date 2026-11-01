"""Phase 4: issue, store and score live forecasts (run by the daily workflow).

    python -m models.daily                  # forecast, store, score, check
    python -m models.daily --no-store       # forecast.json only, database untouched

Steps:
  1. Forecast from the newest inflow with the production model (models/artifacts/LATEST).
  2. Store each forecast once in the database's write-once `forecasts` table and
     append it to data/forecasts/issued.csv. The CSV's git history is a public
     timestamp: anyone can check a forecast existed before its day was reported.
  3. Score every stored forecast whose target day now has a reported inflow,
     against the reported value and against persistence (no change).
  4. Monitor: alert when the 90% intervals catch too few outcomes, when the
     model does worse than persistence, or when recent inputs are more unusual
     (population stability index) than the same season was in any training year.

Writes web/public/data/{forecast,accuracy,monitor}.json. Exit code 2 means a
monitor alert: the workflow then opens a GitHub issue.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import select

from pipeline import db
from pipeline.config import DATA_DIR, ROOT, WEATHER_DIR

from .data import HISTORY_JSON
from .predict import Production, build_doc, latest_version, live_frames

OUT_DIR = ROOT / "web" / "public" / "data"
ISSUED_CSV = DATA_DIR / "forecasts" / "issued.csv"
QCOLS = ["p05", "p10", "p25", "p50", "p75", "p90", "p95"]
CSV_FIELDS = ["issued_on", "station_id", "horizon_days", "model_version", "target_date", *QCOLS,
              "inflow_at_issue", "created_at"]
LEVEL_COLS = {50: ("p25", "p75"), 80: ("p10", "p90"), 90: ("p05", "p95")}

# Monitor thresholds
MONITOR_WINDOW_DAYS = 60
MIN_SCORED = 30
MIN_COVERAGE90 = 0.75
MIN_SKILL = -0.10
PSI_WATCH = 0.10      # PSI below this never counts as drift
MIN_PAST_YEARS = 6    # past windows needed before drift can alert
PSI_FEATURES = ["log_q0", "pdd7", "precip7"]  # flow, melt, rain (tmean7 and d7 mostly duplicate these)
DRIFT_MARGIN = 1.5    # alert only when PSI is 50% above the most unusual past year


# 1-2. Issue and store -------------------------------------------------------

def forecast_rows(doc: dict, created_at: datetime) -> list[dict]:
    rows = []
    for sid, s in doc["stations"].items():
        for f in s["forecasts"]:
            rows.append({"issued_on": date.fromisoformat(s["issue_date"]), "station_id": sid,
                         "horizon_days": int(f["h"]), "model_version": doc["model_version"],
                         "target_date": date.fromisoformat(f["target_date"]),
                         **{q: float(f[q]) for q in QCOLS}, "inflow_at_issue": float(s["inflow_today"]),
                         "created_at": created_at})
    return rows


def store(engine, rows: list[dict], log_csv: Path = ISSUED_CSV) -> list[dict]:
    """Insert forecasts not stored yet (a rerun on the same day adds nothing)."""
    db.migrate(engine)
    with engine.begin() as conn:
        new = db.insert_new(conn, db.forecasts, rows, ["issued_on", "station_id", "horizon_days", "model_version"])
    if new:
        log_csv.parent.mkdir(parents=True, exist_ok=True)
        fresh = not log_csv.exists()
        with log_csv.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if fresh:
                w.writeheader()
            for r in new:
                w.writerow({**r, "created_at": r["created_at"].isoformat(timespec="seconds")})
    return new


# 3. Score -------------------------------------------------------------------

def scored_forecasts(engine) -> pd.DataFrame:
    """Every stored forecast whose target day has a reported inflow.

    If a day's forecast was issued twice (e.g. a new model version the same day),
    only the first one issued counts.
    """
    with engine.connect() as conn:
        fc = pd.DataFrame(conn.execute(select(db.forecasts)).mappings().all())
        rows = [dict(r) for r in conn.execute(
            select(db.observations.c.station_id, db.observations.c.date, db.observations.c.inflow_cusecs)
            .where(db.observations.c.inflow_cusecs.is_not(None))).mappings().all()]
    from pipeline.queries import apply_review  # reviewed corrections (Phase 5) count when scoring

    obs = pd.DataFrame([r for r in apply_review(rows) if r.get("inflow_cusecs") is not None],
                       columns=["station_id", "date", "inflow_cusecs"])
    if fc.empty:
        return fc
    fc = (fc.sort_values("created_at")
            .drop_duplicates(["issued_on", "station_id", "horizon_days"], keep="first"))
    if obs.empty:
        return fc.iloc[0:0].assign(actual=pd.Series(dtype=float))
    obs = obs.rename(columns={"date": "target_date", "inflow_cusecs": "actual"})
    for d in (fc, obs):
        d["target_date"] = pd.to_datetime(d["target_date"])
    fc["issued_on"] = pd.to_datetime(fc["issued_on"])
    return fc.merge(obs, on=["station_id", "target_date"], how="inner")


def score_table(s: pd.DataFrame) -> dict:
    """MAE vs persistence and interval coverage for a set of scored forecasts."""
    if s.empty:
        return {"n": 0}
    y = s["actual"].to_numpy(float)
    mae = float(np.mean(np.abs(y - s["p50"])))
    has_q0 = s["inflow_at_issue"].notna()
    mae_p = float(np.mean(np.abs(y[has_q0] - s.loc[has_q0, "inflow_at_issue"]))) if has_q0.any() else None
    out = {"n": int(len(s)), "mae_model": round(mae),
           "mae_persistence": round(mae_p) if mae_p is not None else None,
           "skill_vs_persistence": round(1 - mae / mae_p, 4) if mae_p else None}  # undefined if persistence was perfect
    for lvl, (lo, hi) in LEVEL_COLS.items():
        ok = s[lo].notna() & s[hi].notna()
        out[f"coverage{lvl}"] = round(float(((y[ok] >= s.loc[ok, lo]) & (y[ok] <= s.loc[ok, hi])).mean()), 4) if ok.any() else None
    return out


def accuracy_doc(scored: pd.DataFrame, all_forecasts: int, first_issued, prod_meta: dict, recent_days: int = 45) -> dict:
    by_h = {int(h): score_table(g) for h, g in scored.groupby("horizon_days")} if len(scored) else {}
    by_station = {sid: score_table(g) for sid, g in scored.groupby("station_id")} if len(scored) else {}
    by_version = {v: score_table(g) for v, g in scored.groupby("model_version")} if len(scored) else {}
    recent = []
    if len(scored):
        cut = scored["target_date"].max() - pd.Timedelta(days=recent_days)
        r = scored[scored["target_date"] > cut].sort_values(["target_date", "station_id", "horizon_days"])
        for _, x in r.iterrows():
            recent.append({"station": x["station_id"], "h": int(x["horizon_days"]),
                           "issued_on": str(x["issued_on"].date()), "target_date": str(x["target_date"].date()),
                           **{q: (round(x[q]) if pd.notna(x[q]) else None) for q in QCOLS},
                           "persistence": round(x["inflow_at_issue"]) if pd.notna(x["inflow_at_issue"]) else None,
                           "actual": round(x["actual"]), "model_version": x["model_version"]})
    backtest = {h: {k: v.get(k) for k in ("skill_vs_persistence", "coverage50", "coverage80", "coverage90")}
                for h, d in prod_meta.get("horizons", {}).items() if (v := d.get("backtest"))}
    return {"generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "forecasts_issued": int(all_forecasts), "first_issued": str(first_issued) if first_issued else None,
            "scored": int(len(scored)), "overall": score_table(scored), "by_horizon": by_h,
            "by_station": by_station, "by_model_version": by_version, "recent": recent,
            "backtest": backtest}


# 4. Monitor -----------------------------------------------------------------

def psi(reference: np.ndarray, current: np.ndarray, bins: int = 5, eps: float = 1e-4) -> float | None:
    """Population stability index over quantile bins of the reference.
    The usual rule of thumb (0.25 = a real shift) assumes independent samples;
    see drift() for why this project judges it against past years instead."""
    reference, current = reference[~np.isnan(reference)], current[~np.isnan(current)]
    if len(reference) < 50 or len(current) < 20:
        return None
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return None
    edges[0], edges[-1] = -np.inf, np.inf
    r = np.histogram(reference, edges)[0] / len(reference) + eps
    c = np.histogram(current, edges)[0] / len(current) + eps
    return float(np.sum((c - r) * np.log(c / r)))


def same_season(index: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp) -> np.ndarray:
    """True for dates whose day of year falls in start..end (wrapping over New Year)."""
    d0, d1, doy = start.day_of_year, end.day_of_year, index.day_of_year
    return (doy >= d0) & (doy <= d1) if d0 <= d1 else (doy >= d0) | (doy <= d1)


def window_psi(f: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, feature: str,
               exclude_year: int | None = None, until: pd.Timestamp | None = None) -> float | None:
    cur = f.loc[start:end, feature].to_numpy(float)
    ref = f[same_season(f.index, start, end)]
    if until is not None:
        ref = ref[ref.index <= until]
    if exclude_year is not None:
        ref = ref[ref.index.year != exclude_year]
    return psi(ref[feature].to_numpy(float), cur)


def drift(frames: dict, train_to: str, window: int = MONITOR_WINDOW_DAYS) -> dict:
    """How unusual the last `window` days of inputs are, judged against history.

    A season is autocorrelated, so one warm autumn shifts every temperature
    feature at once and a fixed PSI cut-off (0.25) trips in most years. Instead,
    each training year's same window is scored against the other training years,
    and the current window is compared with that spread: an alert only when it
    is DRIFT_MARGIN times more unusual than the most unusual training year.
    """
    out = {}
    cutoff = pd.Timestamp(train_to)
    for sid, f in frames.items():
        end = f["log_q0"].dropna().index.max()
        start = end - pd.Timedelta(days=window - 1)
        years = sorted({y for y in f.index[f.index <= cutoff].year})
        feats = {}
        for k in PSI_FEATURES:
            if k not in f:
                continue
            cur = window_psi(f, start, end, k, until=cutoff)
            hist = []
            for y in years:
                e = end.replace(year=y) if not (end.month == 2 and end.day == 29) else end.replace(year=y, day=28)
                s0 = e - pd.Timedelta(days=window - 1)
                if e > cutoff:
                    continue
                v = window_psi(f, s0, e, k, exclude_year=y, until=cutoff)
                if v is not None:
                    hist.append(v)
            feats[k] = {"psi": round(cur, 3) if cur is not None else None,
                        "past_years": len(hist),
                        "past_max": round(max(hist), 3) if hist else None,
                        "past_p75": round(float(np.percentile(hist, 75)), 3) if hist else None}
        out[sid] = feats
    return out


def monitor(scored: pd.DataFrame, drift_psi: dict, today: date) -> dict:
    alerts, watch = [], []
    recent = scored[scored["target_date"] > pd.Timestamp(today) - pd.Timedelta(days=MONITOR_WINDOW_DAYS)] if len(scored) else scored
    window = {}
    if len(recent):
        for h, g in recent.groupby("horizon_days"):
            t = score_table(g)
            window[int(h)] = t
            if t["n"] < MIN_SCORED:
                continue
            if t.get("coverage90") is not None and t["coverage90"] < MIN_COVERAGE90:
                alerts.append(f"{h}-day forecasts: the 90% range caught only {t['coverage90']:.0%} of the last "
                              f"{t['n']} outcomes (alert below {MIN_COVERAGE90:.0%}).")
            if h >= 3 and t.get("skill_vs_persistence") is not None and t["skill_vs_persistence"] < MIN_SKILL:
                alerts.append(f"{h}-day forecasts are {-t['skill_vs_persistence']:.0%} worse than assuming no change "
                              f"over the last {t['n']} outcomes.")
    for sid, feats in drift_psi.items():
        for k, d in feats.items():
            v, mx, p75, n = d.get("psi"), d.get("past_max"), d.get("past_p75"), d.get("past_years", 0)
            if v is None or v <= PSI_WATCH or mx is None or n < MIN_PAST_YEARS:
                continue
            if v > DRIFT_MARGIN * mx:
                alerts.append(f"{sid.title()}: `{k}` over the last {MONITOR_WINDOW_DAYS} days is far more unusual than in "
                              f"any of {n} training years (PSI {v:.2f}, previous maximum {mx:.2f}).")
            elif v > mx or v > p75:
                watch.append(f"{sid}: {k} PSI {v:.2f} ({'beyond' if v > mx else 'top quarter of'} {n} past years)")
    return {"generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "alert" if alerts else ("watch" if watch else "ok"), "alerts": alerts, "watch": watch,
            "window_days": MONITOR_WINDOW_DAYS, "recent_scores": window, "drift": drift_psi,
            "thresholds": {"min_coverage90": MIN_COVERAGE90, "min_skill": MIN_SKILL, "min_scored": MIN_SCORED,
                           "psi_floor": PSI_WATCH, "drift_rule": f"alert when PSI exceeds {DRIFT_MARGIN} x the most unusual training year"}}


# Runner -------------------------------------------------------------------

def run(engine=None, *, store_forecasts: bool = True, today: date | None = None, artifacts: Path | None = None,
        history: Path = HISTORY_JSON, weather_dir: Path = WEATHER_DIR, out_dir: Path = OUT_DIR,
        log_csv: Path = ISSUED_CSV) -> int:
    today = today or date.today()
    prod = Production(artifacts or latest_version())
    frames, adj = live_frames(history, weather_dir)
    doc = build_doc(prod, frames, adj, today)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "forecast.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(f"Model {prod.meta['version']}: forecasts from "
          + ", ".join(f"{sid} {s['issue_date']}" for sid, s in doc["stations"].items()))
    for sid, s in doc["stations"].items():
        for w in s["warnings"]:
            print(f"  ! {sid}: {w}")
    if not store_forecasts:
        return 0

    engine = engine or db.get_engine()
    new = store(engine, forecast_rows(doc, datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)), log_csv)
    print(f"Stored {len(new)} new forecast(s)" + ("" if new else " (already issued from this date)"))

    scored = scored_forecasts(engine)
    with engine.connect() as conn:
        rows = conn.execute(select(db.forecasts.c.issued_on)).scalars().all()
    acc = accuracy_doc(scored, len(rows), min(rows) if rows else None, prod.meta)
    (out_dir / "accuracy.json").write_text(json.dumps(acc, indent=1), encoding="utf-8")
    o = acc["overall"]
    print(f"Scored {acc['scored']} of {acc['forecasts_issued']} forecasts"
          + (f": MAE {o['mae_model']:,} vs persistence {o['mae_persistence']:,}, 90% range caught "
             f"{o['coverage90']:.0%}" if o.get("n") and o.get("mae_persistence") else ""))

    train_to = min(h["train_to"] for h in prod.meta["horizons"].values())
    mon = monitor(scored, drift(frames, train_to), today)
    (out_dir / "monitor.json").write_text(json.dumps(mon, indent=1), encoding="utf-8")
    print(f"Monitor: {mon['status']}")
    for a in mon["alerts"]:
        print(f"  ALERT {a}")
    return 2 if mon["alerts"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-store", action="store_true", help="write forecast.json only")
    args = ap.parse_args(argv)
    return run(store_forecasts=not args.no_store)


if __name__ == "__main__":
    sys.exit(main())
