"""Issue today's forecasts with the production models.

    python -m models.predict                 # writes web/public/data/forecast.json
    python -m models.predict --print         # also prints a table

Uses models/artifacts/<LATEST>/, the newest history in history.json, and ERA5
weather topped up with the bias-corrected recent days from
`python -m pipeline.fetch_weather --recent` (run that first for fresh weather).

Each forecast carries calibrated quantiles (p05..p95: the 50%, 80% and 90%
intervals) and the three biggest drivers of its median, in plain words.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.config import ROOT, WEATHER_DIR

from .data import HISTORY_JSON, load_history, load_weather
from .features import WEATHER_FEATURES, latest_rows, station_frame
from .train import LEVELS, load_booster

ARTIFACTS_DIR = ROOT / "models" / "artifacts"
FORECAST_JSON = ROOT / "web" / "public" / "data" / "forecast.json"

FEATURE_LABELS = {
    "station": "which dam", "log_q0": "today's inflow", "observed0": "whether today's figure was reported",
    "d1": "change since yesterday", "d3": "change over 3 days", "d7": "change over a week",
    "d14": "change over two weeks", "rm3": "the 3-day average", "rm7": "the 7-day average",
    "rm30": "the 30-day average", "n_obs14": "how many recent days were reported", "level0": "the lake level",
    "dl1": "lake level change since yesterday", "dl7": "lake level change over a week",
    "doy": "the time of year", "pdd3": "warmth over 3 days (melt)", "pdd7": "warmth over a week (melt)",
    "pdd14": "warmth over two weeks (melt)", "tmax3": "daytime highs over 3 days",
    "tmean7": "average temperature over a week", "dtemp7": "warming over the last week",
    "precip3": "rain and snow over 3 days", "precip7": "rain and snow over a week",
    "precip14": "rain and snow over two weeks", "snow7": "snowfall over a week", "snow30": "snowfall over 30 days",
}
MAX_WEATHER_CARRY_DAYS = 7
# calibrated quantile name -> (level, side)
QUANTILE_SLOTS = {"p05": (0.90, "lo"), "p10": (0.80, "lo"), "p25": (0.50, "lo"),
                  "p75": (0.50, "hi"), "p90": (0.80, "hi"), "p95": (0.90, "hi")}


def latest_version(artifacts_dir: Path = ARTIFACTS_DIR) -> Path:
    latest = artifacts_dir / "LATEST"
    if not latest.exists():
        raise SystemExit("No trained model yet: run  python -m models.backtest  first.")
    return artifacts_dir / latest.read_text().strip()


class Production:
    def __init__(self, art_dir: Path):
        self.dir = art_dir
        self.meta = json.loads((art_dir / "meta.json").read_text(encoding="utf-8"))
        self.features = self.meta["features"]
        self.horizons = [int(h) for h in self.meta["horizons"]]
        self.boosters = {h: {a: load_booster(art_dir / f"h{h}_q{int(round(a * 100)):02d}.txt.gz")
                             for a in self.meta["quantiles"]} for h in self.horizons}

    def qhat(self, h: int, lvl: float) -> float:
        return float(self.meta["horizons"][str(h)]["qhat"].get(str(int(lvl * 100)), 0.0))

    def predict(self, X: pd.DataFrame, h: int) -> pd.DataFrame:
        """Calibrated quantiles of z = log(q[t+h] / q[t]), monotone in the quantile."""
        Xf = X[self.features]
        raw = {a: b.predict(Xf) for a, b in self.boosters[h].items()}
        out = pd.DataFrame(index=X.index)
        out["p50"] = raw[0.5]
        for name, (lvl, side) in QUANTILE_SLOTS.items():
            lo_a, hi_a = {0.50: (0.25, 0.75), 0.80: (0.10, 0.90), 0.90: (0.05, 0.95)}[lvl]
            lo, hi = np.minimum(raw[lo_a], raw[hi_a]), np.maximum(raw[lo_a], raw[hi_a])
            out[name] = lo - self.qhat(h, lvl) if side == "lo" else hi + self.qhat(h, lvl)
        cols = ["p05", "p10", "p25", "p50", "p75", "p90", "p95"]
        out[cols] = np.maximum.accumulate(out[cols].to_numpy(), axis=1)
        return out[cols]

    def drivers(self, X: pd.DataFrame, h: int, top: int = 3, min_pct: float = 0.5) -> list[list[dict]]:
        """Biggest SHAP contributions to the median, skipping features with no value
        (LightGBM still routes a missing value down a branch, which is not a cause)."""
        contrib = self.boosters[h][0.5].predict(X[self.features], pred_contrib=True)[:, :-1]
        out = []
        for (_, xrow), row in zip(X[self.features].iterrows(), contrib):
            c = pd.Series(row, index=self.features)
            c = c[xrow.notna().to_numpy()]
            doy = c.reindex(["doy_sin", "doy_cos"]).fillna(0.0).sum()
            c = c.drop(["doy_sin", "doy_cos"], errors="ignore")
            c["doy"] = doy
            c = c.drop("station", errors="ignore")
            c = c[np.abs(np.expm1(c)) * 100 >= min_pct]
            biggest = c.reindex(c.abs().sort_values(ascending=False).index)[:top]
            out.append([{"feature": k, "label": FEATURE_LABELS.get(k, k),
                         "effect_pct": round(float(np.expm1(v)) * 100, 1)} for k, v in biggest.items()])
        return out


def driver_text(drivers: list[dict]) -> str:
    if not drivers:
        return "No single factor moves this forecast by more than half a percent."
    parts = [f"{d['label']} ({'+' if d['effect_pct'] >= 0 else '−'}{abs(d['effect_pct']):.0f}%)" for d in drivers]
    return "Biggest influences: " + ", ".join(parts)


def forecast(frames: dict, prod: Production, today: date | None = None) -> dict:
    today = today or date.today()
    rows = latest_rows(frames).set_index("sid", drop=False)
    out = {}
    for sid, row in rows.iterrows():
        X = rows.loc[[sid]].copy()
        q0 = float(row["q0"])
        issue = pd.Timestamp(row["issue_date"]).date()
        warnings = []
        age = (today - issue).days
        if age > 2:
            warnings.append(f"The newest inflow figure is {age} days old, so these forecasts start from {issue}.")
        w_missing = [c for c in WEATHER_FEATURES if pd.isna(row.get(c))]
        f = frames[sid]
        complete = f[WEATHER_FEATURES].dropna().index
        complete = complete[complete <= pd.Timestamp(issue)]
        if w_missing and len(complete) and (pd.Timestamp(issue) - complete.max()).days <= MAX_WEATHER_CARRY_DAYS:
            src = complete.max()
            X.loc[sid, WEATHER_FEATURES] = f.loc[src, WEATHER_FEATURES].to_numpy()
            warnings.append(f"The newest weather is {(pd.Timestamp(issue) - src).days} days behind; weather features "
                            f"are carried forward from {src.date()}.")
            w_missing = []
        if w_missing:
            warnings.append(f"Weather is incomplete for this date ({len(w_missing)} of {len(WEATHER_FEATURES)} "
                            "weather features missing); the forecast leans on flows alone.")
        fc = []
        for h in prod.horizons:
            z = prod.predict(X, h).iloc[0]
            drv = prod.drivers(X, h)[0]
            item = {"h": h, "target_date": str(issue + pd.Timedelta(days=h).to_pytimedelta())}
            item.update({k: round(q0 * float(np.exp(v))) for k, v in z.items()})
            item["drivers"] = drv
            item["explanation"] = driver_text(drv)
            fc.append(item)
        wthrough = frames[sid]["_pdd"].dropna().index.max() if "_pdd" in frames[sid] else None
        out[sid] = {"issue_date": str(issue), "inflow_today": round(q0), "observed_today": bool(row["observed0"]),
                    "data_age_days": age, "weather_through": str(wthrough.date()) if wthrough is not None and pd.notna(wthrough) else None,
                    "warnings": warnings, "forecasts": fc}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", type=Path, default=HISTORY_JSON)
    ap.add_argument("--weather-dir", type=Path, default=WEATHER_DIR)
    ap.add_argument("--artifacts", type=Path, default=None, help="a version folder (default: LATEST)")
    ap.add_argument("--out", type=Path, default=FORECAST_JSON)
    ap.add_argument("--no-recent", action="store_true", help="ERA5 only, ignore recent_<point>.csv")
    ap.add_argument("--print", action="store_true", dest="show")
    args = ap.parse_args(argv)

    prod = Production(args.artifacts or latest_version())
    adj: dict = {}
    hist = load_history(args.history)
    weather = load_weather(args.weather_dir, recent=not args.no_recent, adjustments=adj)
    frames = {sid: station_frame(sid, h, weather.get(sid)) for sid, h in hist.items()}
    stations = forecast(frames, prod)
    doc = {"model_version": prod.meta["version"],
           "issued_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
           "horizons": prod.horizons, "levels": [50, 80, 90], "units": "cusecs",
           "weather_adjustments": adj, "stations": stations,
           "skill": {h: (prod.meta["horizons"][str(h)].get("backtest") or {}).get("skill_vs_persistence")
                     for h in prod.horizons}}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(f"Model {prod.meta['version']}: forecasts written to {args.out}")
    for sid, s in stations.items():
        print(f"\n{sid.title()}: {s['inflow_today']:,} cusecs on {s['issue_date']} (weather through {s['weather_through']})")
        for w in s["warnings"]:
            print(f"  ! {w}")
        if args.show:
            print("   h  target       p05      p25      p50      p75      p95")
            for f in s["forecasts"]:
                print(f"  {f['h']:>2}  {f['target_date']}  {f['p05']:>7,}  {f['p25']:>7,}  {f['p50']:>7,}  "
                      f"{f['p75']:>7,}  {f['p95']:>7,}")
            print("  " + s["forecasts"][0]["explanation"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
