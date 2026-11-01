"""Phase 5: find suspicious values in the daily history and queue them for review.

    python -m pipeline.anomaly              # detect, write web/public/data/review.json
    python -m pipeline.anomaly --print      # also list what is pending

The history is rebuilt from news stories, so most errors are typing errors in
a story: a digit wrong or dropped, two digits swapped, inflow and outflow
swapped, one dam's figure given to the other. Four checks look for them:

  local     robust z-score against a trend line (Theil-Sen) through the 7 days
            either side, scaled by the MAD around that line
  seasonal  STL decomposition (yearly cycle removed), then the same spike test
            on what is left, so steep seasonal rises are not mistaken for jumps
  isolation Isolation Forest over each day's changes and residuals together
  balance   water balance: the lake level must move the way inflow minus
            outflow says it should; a swapped or mistyped flow breaks it

A value is queued when the evidence adds up (two strong signals, or one
strong and two moderate), or the local check alone is extreme, unless the
day before or after reports nearly the same figure (a typo rarely repeats). The settings
were tuned on the real history with planted errors (pipeline.anomaly_eval). Nothing is
changed automatically: decisions go in data/review/decisions.csv (see
pipeline.review) and are applied whenever the data is read.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import DATA_DIR, INVENTORY_DIR, ROOT, STATIONS

FIELDS = ("inflow_cusecs", "outflow_cusecs", "level_ft")
LOG_FIELDS = {"inflow_cusecs", "outflow_cusecs"}
FIELD_LABELS = {"inflow_cusecs": "inflow", "outflow_cusecs": "outflow", "level_ft": "level"}
UNITS = {"inflow_cusecs": "cusecs", "outflow_cusecs": "cusecs", "level_ft": "ft"}

WINDOW_DAYS = 7          # neighbours either side
WIDE_WINDOW_DAYS = 15    # used where fewer than MIN_NEIGHBOURS fall within a week
MIN_NEIGHBOURS = 4
SCALE_FLOOR = {"inflow_cusecs": 0.04, "outflow_cusecs": 0.12, "level_ft": 0.35}
# ~4% of inflow; 12% of outflow (releases change in steps by operator decision); 0.35 ft of level
Z_FLAG = 5.0             # a z-score this high counts as one piece of evidence
Z_MODERATE = 3.5         # ...this high, as half
MIN_EVIDENCE = 2.0       # queue a value once the evidence adds up to this
Z_EXTREME = 10.0         # the local check alone is enough at this score
BALANCE_Z = 6.0
BALANCE_BIG = 15.0
# Two stories rarely repeat the same typo on consecutive days, so a value within
# this of the day before or after counts as confirmed, unless wildly off.
TWIN_TOLERANCE = {"inflow_cusecs": 0.03, "outflow_cusecs": 0.03, "level_ft": 0.3}
TWIN_OVERRIDE_Z = 25.0
IF_CONTAMINATION = 0.01
CONFLICT_MIN_GAP = 0.05  # outlets disagreeing by more than 5% go to review

REVIEW_JSON = ROOT / "web" / "public" / "data" / "review.json"
DECISIONS_CSV = DATA_DIR / "review" / "decisions.csv"
CONFLICTS_CSV = INVENTORY_DIR / "news_conflicts.csv"
DECISION_FIELDS = ["kind", "ref", "station_id", "date", "field", "decision", "value", "note", "decided_on"]


# Data ---------------------------------------------------------------------

def frames_from_rows(rows) -> dict[str, pd.DataFrame]:
    """{station: DataFrame on a complete daily index with level_ft, inflow_cusecs, outflow_cusecs}."""
    df = pd.DataFrame(rows)
    out = {}
    if df.empty:
        return out
    df["date"] = pd.to_datetime(df["date"])
    for sid, g in df.groupby("station_id"):
        g = g.set_index("date").sort_index()
        g = g[~g.index.duplicated(keep="last")]
        idx = pd.date_range(g.index.min(), g.index.max(), freq="D")
        out[sid] = g.reindex(idx)[list(FIELDS)].astype(float)
    return out


def transformed(s: pd.Series, field: str) -> pd.Series:
    return np.log(s.clip(lower=1.0)) if field in LOG_FIELDS else s


# Checks ---------------------------------------------------------------------

def neighbour_z(x: pd.Series, field: str) -> tuple[pd.Series, pd.Series]:
    """Robust z of each observed value against a trend line through the
    observed values within WINDOW_DAYS either side (the value itself excluded).

    The line is a Theil-Sen fit (median of pairwise slopes), so a lake rising a
    foot a day or inflow climbing through the melt is not mistaken for spread;
    the scale is the MAD of the neighbours around that line. Returns (z, expected).
    """
    vals = x.to_numpy(float)
    obs = np.flatnonzero(~np.isnan(vals))
    z = np.full(len(vals), np.nan)
    exp = np.full(len(vals), np.nan)
    floor = SCALE_FLOOR[field]
    for i in obs:
        for w in (WINDOW_DAYS, WIDE_WINDOW_DAYS):  # widen where the history is thin
            lo, hi = max(0, i - w), min(len(vals), i + w + 1)
            t = np.r_[np.arange(lo, i), np.arange(i + 1, hi)]
            v = vals[t]
            keep = ~np.isnan(v)
            t, v = t[keep], v[keep]
            if len(v) >= MIN_NEIGHBOURS:
                break
        else:
            continue
        dt = t[:, None] - t[None, :]
        dv = v[:, None] - v[None, :]
        pairs = dt > 0
        slope = float(np.median(dv[pairs] / dt[pairs])) if pairs.any() else 0.0
        # one-sided neighbours (e.g. the first day after a gap): don't extrapolate a trend
        if t.min() > i or t.max() < i:
            slope = 0.0
        resid = v - slope * (t - i)
        centre = float(np.median(resid))
        mad = 1.4826 * float(np.median(np.abs(resid - centre)))
        z[i] = (vals[i] - centre) / max(mad, floor)
        exp[i] = centre
    return pd.Series(z, x.index), pd.Series(exp, x.index)


def seasonal_residual(x: pd.Series) -> pd.Series | None:
    """STL residual + trend-free spike series: the yearly cycle and slow trend
    removed, observed days only. None if under two years of span."""
    from statsmodels.tsa.seasonal import STL

    obs = x.dropna()
    if len(obs) < 200 or (obs.index.max() - obs.index.min()).days < 2 * 365 + 30:
        return None
    filled = x.loc[obs.index.min():obs.index.max()].interpolate(limit_area="inside")
    fit = STL(filled, period=365, seasonal=7, trend=367, robust=True,
              seasonal_jump=7, trend_jump=7, low_pass_jump=7).fit()
    deseason = (filled - fit.seasonal)
    return deseason.where(x.loc[deseason.index].notna())


def seasonal_z(x: pd.Series, field: str) -> pd.Series:
    r = seasonal_residual(x)
    if r is None:
        return pd.Series(np.nan, x.index)
    z, _ = neighbour_z(r.reindex(x.index), field)
    return z


def balance_z(f: pd.DataFrame) -> pd.Series:
    """Water balance: day-on-day level change against inflow minus outflow.

    The level rise per cusec of net inflow depends on the lake's area, which
    grows with level, so the ratio is estimated separately for each fifth of
    the level range (median of change / net flow on days with a clear net flow).
    """
    lvl, qin, qout = f["level_ft"], f["inflow_cusecs"], f["outflow_cusecs"]
    dl = lvl - lvl.shift(1)
    net = qin - qout
    ok = dl.notna() & net.notna() & lvl.shift(1).notna()
    z = pd.Series(np.nan, f.index)
    if ok.sum() < 60:
        return z
    bins = pd.qcut(lvl.shift(1)[ok], 5, labels=False, duplicates="drop")
    pred = pd.Series(np.nan, f.index)
    for b in np.unique(bins):
        sel = ok & (bins.reindex(f.index) == b)
        clear = sel & (net.abs() > 15000)
        if clear.sum() < 10:
            continue
        slope = float(np.median(dl[clear] / net[clear]))
        pred[sel] = slope * net[sel]
    resid = (dl - pred)[pred.notna()]
    if len(resid) < 30:
        return z
    mad = 1.4826 * np.median(np.abs(resid - np.median(resid)))
    z[resid.index] = (resid - np.median(resid)) / max(mad, 0.15)
    return z


def isolation_flags(f: pd.DataFrame, local_z: dict[str, pd.Series], bal: pd.Series) -> pd.Series:
    """Isolation Forest on each fully reported day: residuals and day-on-day
    changes of the three fields plus the water-balance residual. Returns the
    implicated field for flagged days (else None)."""
    from sklearn.ensemble import IsolationForest

    cols = {}
    for fld in FIELDS:
        t = transformed(f[fld], fld)
        cols[f"z_{fld}"] = local_z[fld]
        cols[f"d_{fld}"] = (t - t.shift(1)) / SCALE_FLOOR[fld]
    cols["balance"] = bal
    X = pd.DataFrame(cols).dropna()
    out = pd.Series(None, f.index, dtype=object)
    if len(X) < 100:
        return out
    model = IsolationForest(n_estimators=300, contamination=IF_CONTAMINATION, random_state=0).fit(X)
    flagged = X.index[model.predict(X) == -1]
    for d in flagged:
        row = X.loc[d]
        score = {fld: max(abs(row[f"z_{fld}"]), abs(row[f"d_{fld}"]) / 2) for fld in FIELDS}
        out[d] = max(score, key=score.get)
    return out


# Ensemble -------------------------------------------------------------------

@dataclass
class Flag:
    station_id: str
    date: str
    field: str
    value: float
    expected: float | None
    checks: list[str]
    score: float

    @property
    def key(self) -> tuple[str, str, str]:
        return self.station_id, self.date, self.field


def score_station(sid: str, f: pd.DataFrame, use_seasonal: bool = True) -> pd.DataFrame:
    """One row per reported value: local and seasonal z, and whether the
    Isolation Forest and the water balance implicate it."""
    local, expected, seasonal = {}, {}, {}
    for fld in FIELDS:
        t = transformed(f[fld], fld)
        local[fld], e = neighbour_z(t, fld)
        expected[fld] = np.exp(e) if fld in LOG_FIELDS else e
        seasonal[fld] = seasonal_z(t, fld) if use_seasonal else pd.Series(np.nan, f.index)
    bal = balance_z(f)
    iso = isolation_flags(f, local, bal)
    # a broken balance implicates whichever field looks most out of line that day
    lz = pd.DataFrame({k: local[k].abs() for k in FIELDS})
    culprit = lz.fillna(0).idxmax(axis=1).where(lz.max(axis=1) > 2, "level_ft")
    rows = []
    for fld in FIELDS:
        obs = f[fld].dropna().index
        # corroborated: the day before or after reports nearly the same figure
        v = f[fld]
        tol = TWIN_TOLERANCE[fld]
        close = lambda o: ((v - o).abs() / (v.abs() if fld in LOG_FIELDS else 1.0)) <= tol  # noqa: E731
        twin = close(v.shift(1)) | close(v.shift(-1))
        rows.append(pd.DataFrame({
            "twin": twin.reindex(obs).fillna(False).to_numpy(bool),
            "station_id": sid, "date": obs, "field": fld, "value": f.loc[obs, fld].to_numpy(),
            "expected": expected[fld].reindex(obs).to_numpy(),
            "local": local[fld].reindex(obs).to_numpy(), "seasonal": seasonal[fld].reindex(obs).to_numpy(),
            "isolation": (iso.reindex(obs) == fld).to_numpy(),
            "balance": ((bal.reindex(obs).abs() > BALANCE_Z) & (culprit.reindex(obs) == fld)).to_numpy(),
            "balance_z": bal.reindex(obs).to_numpy(),
        }))
    return pd.concat(rows, ignore_index=True)


def evidence(sc: pd.DataFrame, z_flag: float = Z_FLAG, z_moderate: float = Z_MODERATE) -> np.ndarray:
    """Evidence that a value is wrong, added up over the checks: a z-score
    above z_flag counts 1, above z_moderate 0.5; the Isolation Forest 1; a
    broken water balance 1 (1.5 when it is off by BALANCE_BIG or more)."""
    loc, sea = sc["local"].abs().fillna(0), sc["seasonal"].abs().fillna(0)
    e = (np.where(loc > z_flag, 1.0, np.where(loc > z_moderate, 0.5, 0.0))
         + np.where(sea > z_flag, 1.0, np.where(sea > z_moderate, 0.5, 0.0))
         + sc["isolation"].astype(float)
         + np.where(sc["balance"], np.where(sc["balance_z"].abs() >= BALANCE_BIG, 1.5, 1.0), 0.0))
    return e


def apply_rule(sc: pd.DataFrame, z_flag: float = Z_FLAG, z_extreme: float = Z_EXTREME,
               z_moderate: float = Z_MODERATE, min_evidence: float = MIN_EVIDENCE) -> list[Flag]:
    """Queue a value when the evidence adds up to min_evidence, or the local check alone is extreme."""
    if sc.empty:
        return []
    e = evidence(sc, z_flag, z_moderate)
    loc = sc["local"].abs()
    twin = sc["twin"] if "twin" in sc else pd.Series(False, sc.index)
    hit = sc[(((e >= min_evidence) | (loc >= z_extreme)) & ~twin) | (twin & (loc >= TWIN_OVERRIDE_Z))]
    out = []
    for r in hit.itertuples():
        checks = [name for name, on in (("local", abs(r.local) > z_moderate), ("seasonal", abs(r.seasonal) > z_moderate),
                                        ("isolation", r.isolation), ("balance", r.balance)) if on]
        score = np.nanmax([abs(r.local), abs(r.seasonal), abs(r.balance_z) if r.balance else 0.0])
        out.append(Flag(r.station_id, str(pd.Timestamp(r.date).date()), r.field, float(r.value),
                        None if pd.isna(r.expected) else float(r.expected), checks, round(float(score), 1)))
    return out


def score(frames: dict[str, pd.DataFrame], use_seasonal: bool = True) -> pd.DataFrame:
    parts = [score_station(sid, f, use_seasonal) for sid, f in frames.items()]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def detect(frames: dict[str, pd.DataFrame], use_seasonal: bool = True, **rule) -> list[Flag]:
    out = apply_rule(score(frames, use_seasonal), **rule)
    return sorted(out, key=lambda x: (x.date, x.station_id, x.field), reverse=True)


# Decisions ------------------------------------------------------------------

def read_decisions(path: Path | None = None) -> list[dict]:
    path = path or DECISIONS_CSV
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("decision")]


def append_decisions(rows: list[dict], path: Path | None = None) -> None:
    path = path or DECISIONS_CSV
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DECISION_FIELDS, extrasaction="ignore")
        if fresh:
            w.writeheader()
        w.writerows(rows)


def corrections(decisions: list[dict]) -> dict[tuple[str, str, str], float | None]:
    """{(station, date, field): replacement} from decisions; None = drop the value.
    The newest decision for a value wins; 'ok' means keep it as reported."""
    out = {}
    for d in decisions:
        if not (d.get("station_id") and d.get("date") and d.get("field")):
            continue
        k = (d["station_id"], d["date"], d["field"])
        if d["decision"] == "bad":
            out[k] = None
        elif d["decision"] == "set" and d.get("value"):
            out[k] = float(d["value"])
        elif d["decision"] == "ok":
            out.pop(k, None)
    return out


def apply_corrections(rows: list[dict], decisions: list[dict] | None = None) -> list[dict]:
    """Apply reviewed decisions to observation rows (dicts with date, station_id, fields).
    'set' on a day with no row adds one (e.g. a quarantined story accepted by hand)."""
    decisions = read_decisions() if decisions is None else decisions
    fixes = corrections(decisions)
    if not fixes:
        return rows
    by_key = {}
    out = []
    for r in rows:
        r = dict(r)
        d = r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"])
        by_key[(r["station_id"], d)] = r
        out.append(r)
    for (sid, d, fld), v in fixes.items():
        r = by_key.get((sid, d))
        if r is None:
            if v is None:
                continue
            r = {"date": date.fromisoformat(d), "station_id": sid, "level_ft": None, "inflow_cusecs": None,
                 "outflow_cusecs": None, "live_storage_maf": None, "source": "review"}
            by_key[(sid, d)] = r
            out.append(r)
        r[fld] = v
    return sorted(out, key=lambda r: (r["station_id"], str(r["date"])))


# Review queue ---------------------------------------------------------------

def context(f: pd.DataFrame, d: str, field: str, days: int = 15) -> dict:
    t = pd.Timestamp(d)
    s = f[field].loc[t - pd.Timedelta(days=days): t + pd.Timedelta(days=days)]
    return {"dates": [str(x.date()) for x in s.index], "values": [None if np.isnan(v) else v for v in s.to_numpy(float)]}


def conflicts(path: Path | None = None, min_gap: float = CONFLICT_MIN_GAP) -> list[dict]:
    path = path or CONFLICTS_CSV
    if not path.exists():
        return []
    out = []
    with path.open(newline="", encoding="utf-8") as fh:
        for i, r in enumerate(csv.DictReader(fh)):
            a, b = float(r["kept_inflow"]), float(r["other_inflow"])
            gap = abs(a - b) / max(a, b)
            if gap > min_gap:
                out.append({"ref": f"c{r['date']}-{r['station_id']}-{i}", "station_id": r["station_id"],
                            "date": r["date"], "field": "inflow_cusecs", "kept": a, "kept_source": r["kept_source"],
                            "kept_link": r["kept_link"], "other": b, "other_source": r["other_source"],
                            "other_link": r["other_link"], "gap": round(gap, 3)})
    return out


def quarantined(engine) -> list[dict]:
    from sqlalchemy import select

    from . import db

    q = db.quarantine.c
    with engine.connect() as conn:
        rows = conn.execute(select(q.id, q.flagged_at, q.source, q.raw_text, q.reason)).mappings().all()
    out = []
    for r in rows:
        try:
            parsed = json.loads(r["raw_text"])
        except (ValueError, TypeError):
            parsed = None
        out.append({"ref": f"q{r['id']}", "flagged_at": str(r["flagged_at"])[:10], "source": r["source"],
                    "reason": r["reason"], "parsed": parsed if isinstance(parsed, dict) else None,
                    "raw_text": None if isinstance(parsed, dict) else str(r["raw_text"])[:400]})
    return out


def build_review(frames, flags: list[Flag], quarantine: list[dict], conflict_rows: list[dict],
                 decisions: list[dict]) -> dict:
    decided_keys = {(d["station_id"], d["date"], d["field"]) for d in decisions if d.get("station_id")}
    decided_refs = {d["ref"] for d in decisions if d.get("ref")}
    pending = []
    for fl in flags:
        if fl.key in decided_keys:
            continue
        pending.append({"kind": "anomaly", "ref": f"a{fl.date}-{fl.station_id}-{fl.field}", "station_id": fl.station_id,
                        "date": fl.date, "field": fl.field, "value": fl.value, "expected": fl.expected,
                        "checks": fl.checks, "score": fl.score,
                        "context": context(frames[fl.station_id], fl.date, fl.field)})
    for q in quarantine:
        if q["ref"] not in decided_refs:
            pending.append({"kind": "quarantine", **q})
    for c in sorted(conflict_rows, key=lambda c: c["date"], reverse=True):
        if c["ref"] not in decided_refs and (c["station_id"], c["date"], c["field"]) not in decided_keys:
            ctx = context(frames[c["station_id"]], c["date"], c["field"]) if c["station_id"] in frames else None
            pending.append({"kind": "conflict", **c, "context": ctx})
    counts = {k: sum(p["kind"] == k for p in pending) for k in ("anomaly", "quarantine", "conflict")}
    return {"generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "pending": pending, "counts": counts, "decided": len(decisions),
            "station_days": int(sum(f[list(FIELDS)].notna().any(axis=1).sum() for f in frames.values())),
            "flagged_total": len(flags),
            "settings": {"window_days": WINDOW_DAYS, "z_flag": Z_FLAG, "z_moderate": Z_MODERATE,
                         "min_evidence": MIN_EVIDENCE, "z_extreme": Z_EXTREME,
                         "balance_z": BALANCE_Z, "isolation_contamination": IF_CONTAMINATION}}


def raw_rows(engine) -> list[dict]:
    """Observations exactly as loaded (no review decisions applied)."""
    from sqlalchemy import select

    from . import db

    o = db.observations.c
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(o.date, o.station_id, o.level_ft, o.inflow_cusecs, o.outflow_cusecs)).mappings().all()]


def main(argv=None) -> int:
    from . import db

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", dest="show")
    ap.add_argument("--out", type=Path, default=REVIEW_JSON)
    args = ap.parse_args(argv)
    engine = db.get_engine()
    frames = frames_from_rows(raw_rows(engine))
    flags = detect(frames)
    review = build_review(frames, flags, quarantined(engine), conflicts(), read_decisions())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(review, indent=1, default=str), encoding="utf-8")
    c = review["counts"]
    print(f"Checked {review['station_days']:,} dam-days: {len(flags)} suspicious values "
          f"({len(flags) / max(review['station_days'], 1) * 1000:.1f} per 1,000 dam-days). "
          f"Waiting for review: {c['anomaly']} anomalies, {c['quarantine']} quarantined stories, "
          f"{c['conflict']} outlet disagreements. Decided so far: {review['decided']}.")
    if args.show:
        for p in review["pending"]:
            if p["kind"] == "anomaly":
                print(f"  {p['date']} {p['station_id']:<8} {FIELD_LABELS[p['field']]:<8} {p['value']:>10,.1f} "
                      f"(expected ~{p['expected'] or float('nan'):,.0f}) checks: {', '.join(p['checks']) or 'extreme'}")
    print(f"Wrote {args.out}. To decide: python -m pipeline.review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
