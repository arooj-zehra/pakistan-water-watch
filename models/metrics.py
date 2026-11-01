"""Baselines and scores, all on the cusecs scale."""
from __future__ import annotations

import numpy as np
import pandas as pd


def mae(y, p) -> float:
    y, p = np.asarray(y, float), np.asarray(p, float)
    ok = ~(np.isnan(y) | np.isnan(p))
    return float(np.mean(np.abs(y[ok] - p[ok]))) if ok.any() else float("nan")


def nse(y, p) -> float:
    """Nash-Sutcliffe efficiency: 1 is perfect, 0 is no better than the mean."""
    y, p = np.asarray(y, float), np.asarray(p, float)
    ok = ~(np.isnan(y) | np.isnan(p))
    if ok.sum() < 2:
        return float("nan")
    y, p = y[ok], p[ok]
    return float(1 - np.sum((y - p) ** 2) / np.sum((y - y.mean()) ** 2))


def climatology_table(frames: dict, before: pd.Timestamp, window: int = 7) -> dict[str, np.ndarray]:
    """{sid: median observed inflow per day of year (1-366)}, from data before `before` only."""
    out = {}
    for sid, f in frames.items():
        raw = f["_raw"][f.index < before].dropna()
        doys = raw.index.day_of_year.to_numpy()
        vals = raw.to_numpy()
        table = np.full(367, np.nan)
        for d in range(1, 367):
            dist = np.minimum(np.abs(doys - d), 366 - np.abs(doys - d))
            sel = vals[dist <= window]
            if len(sel) >= 5:
                table[d] = np.median(sel)
        out[sid] = table
    return out


def baselines(rows: pd.DataFrame, frames: dict, clim: dict) -> pd.DataFrame:
    """Persistence, climatology and seasonal-naive forecasts (cusecs) for each row."""
    out = pd.DataFrame(index=rows.index)
    out["persistence"] = rows["q0"]
    out["climatology"] = [clim[s][d] for s, d in zip(rows["sid"], rows["target_date"].dt.day_of_year)]
    last_year = []
    for s, t in zip(rows["sid"], rows["target_date"]):
        q = frames[s]["q0"]
        last_year.append(q.get(t - pd.Timedelta(days=365), np.nan))
    out["seasonal_naive"] = last_year
    return out


def season(dates: pd.Series) -> pd.Series:
    return np.where(dates.dt.month.between(4, 9), "melt (Apr-Sep)", "low flow (Oct-Mar)")
