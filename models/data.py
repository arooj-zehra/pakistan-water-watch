"""Load the daily history and catchment weather into tidy pandas frames.

History comes from web/public/data/history.json (written by
pipeline.export_static and refreshed by the daily job), so training needs no
database and runs the same on a laptop and in Colab. Weather comes from the
ERA5 CSVs in data/weather/.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline.config import ROOT, WEATHER_DIR, WEATHER_POINTS

HISTORY_JSON = ROOT / "web" / "public" / "data" / "history.json"
STATIONS = ("tarbela", "mangla")


def load_history(path: Path = HISTORY_JSON) -> dict[str, pd.DataFrame]:
    """{station: DataFrame on a complete daily index with inflow, level, observed}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))["stations"]
    out = {}
    for sid in STATIONS:
        s = data.get(sid)
        if not s:
            continue
        df = pd.DataFrame({"date": pd.to_datetime(s["date"]), "inflow": s["inflow_cusecs"],
                           "level": s["level_ft"]}).set_index("date").sort_index()
        df = df[~df.index.duplicated()]
        idx = pd.date_range(df.index.min(), df.index.max(), freq="D")
        df = df.reindex(idx)
        df["inflow"] = pd.to_numeric(df["inflow"], errors="coerce")
        df["level"] = pd.to_numeric(df["level"], errors="coerce")
        df["observed"] = df["inflow"].notna()
        out[sid] = df
    return out


TEMP_COLS = ("tmax_c", "tmin_c", "tmean_c")
WET_COLS = ("precip_mm", "snowfall_cm")


def bias_correct(era5: pd.DataFrame, recent: pd.DataFrame, min_overlap: int = 14) -> tuple[pd.DataFrame, dict]:
    """Shift recent forecast-API weather onto ERA5's scale.

    The forecast model ("best match") runs several degrees off ERA5 in the
    mountains. Over the days both cover: temperatures get ERA5's mean offset,
    precipitation and snowfall ERA5's ratio of totals (clipped to 0.5-2). With
    fewer than min_overlap shared days the values pass through unchanged.
    """
    both = era5.index.intersection(recent.index)
    out, adj = recent.copy(), {}
    if len(both) < min_overlap:
        return out, {"overlap_days": int(len(both))}
    for c in TEMP_COLS:
        off = float((era5.loc[both, c] - recent.loc[both, c]).mean())
        out[c] = recent[c] + off
        adj[c] = round(off, 2)
    for c in WET_COLS:
        tot = float(recent.loc[both, c].sum())
        ratio = float(np.clip(era5.loc[both, c].sum() / tot, 0.5, 2.0)) if tot > 1.0 else 1.0
        out[c] = recent[c] * ratio
        adj[c] = round(ratio, 2)
    adj["overlap_days"] = int(len(both))
    return out, adj


def load_weather(weather_dir: Path = WEATHER_DIR, recent: bool = False,
                 adjustments: dict | None = None) -> dict[str, pd.DataFrame]:
    """{station: daily catchment-mean weather}: tmean, tmax, precip, snow, pdd.

    pdd (positive degree-days) = mean over the catchment's points of
    max(tmean, 0): the melt driver for snow- and glacier-fed rivers.

    recent=True appends the past days from recent_<point>.csv (bias-corrected)
    after ERA5's last day, for live forecasts. The backtest uses ERA5 only.
    Pass a dict as `adjustments` to receive the corrections applied per point.
    """
    per_point = {}
    for p in WEATHER_POINTS:
        path = Path(weather_dir) / f"era5_{p.id}.csv"
        if not path.exists():
            continue
        w = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
        w = w[~w.index.duplicated(keep="last")]
        rpath = Path(weather_dir) / f"recent_{p.id}.csv"
        if recent and rpath.exists():
            r = pd.read_csv(rpath, parse_dates=["date"]).set_index("date").sort_index()
            r = r[r["kind"] == "past"].dropna(subset=["tmean_c"])
            rc, adj = bias_correct(w, r)
            if adjustments is not None:
                adjustments[p.id] = adj
            tail = rc[rc.index > w.index.max()]
            w = pd.concat([w, tail[list(TEMP_COLS + WET_COLS)]])
        per_point[p.id] = (p.catchment, w)
    out = {}
    for sid in STATIONS:
        frames = [w for c, w in per_point.values() if c == sid]
        if not frames:
            continue
        stack = pd.concat(frames, keys=range(len(frames)))
        g = stack.groupby(level=1)
        out[sid] = pd.DataFrame({
            "tmean": g["tmean_c"].mean(),
            "tmax": g["tmax_c"].mean(),
            "precip": g["precip_mm"].mean(),
            "snow": g["snowfall_cm"].mean(),
            "pdd": stack["tmean_c"].clip(lower=0).groupby(level=1).mean(),
        }).asfreq("D")
    return out


def fill_short_gaps(s: pd.Series, max_gap: int = 3) -> pd.Series:
    """Linear fill of gaps of up to max_gap days between two known values."""
    from pipeline.coverage_report import fill_short_gaps as _fill

    return _fill(s, max_gap)


def log1(x):
    return np.log(np.maximum(x, 1.0))
