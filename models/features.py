"""Features at issue date t, targets at t + h.

The issue date t is the morning the day-t release comes out (flows are
24-hour means to 06:00 on t). Rules that keep the backtest honest:

  * flow features use values up to and including t, with gaps of up to 3 days
    filled; longer gaps stay missing (LightGBM handles NaN);
  * weather features use days up to t-1 only (day t is not over at 06:00);
  * targets are OBSERVED values only, never interpolated ones;
  * future weather (t .. t+h-1) is only added in the "oracle" variant, an
    upper bound for what perfect weather forecasts could add.

The target is z = log(q[t+h]) - log(q[t]): predicting z = 0 is exactly the
persistence forecast, so any skill is measured against it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import fill_short_gaps, log1

HORIZONS = (1, 3, 5, 7, 10)
STATION_CODE = {"tarbela": 0, "mangla": 1}

FLOW_FEATURES = ["station", "log_q0", "observed0", "d1", "d3", "d7", "d14", "rm3", "rm7", "rm30",
                 "n_obs14", "level0", "dl1", "dl7", "doy_sin", "doy_cos"]
WEATHER_FEATURES = ["pdd3", "pdd7", "pdd14", "tmax3", "tmean7", "dtemp7", "precip3", "precip7",
                    "precip14", "snow7", "snow30"]
FUTURE_FEATURES = ["pdd_fut", "precip_fut"]


def feature_names(future_weather: bool = False) -> list[str]:
    return FLOW_FEATURES + WEATHER_FEATURES + (FUTURE_FEATURES if future_weather else [])


def station_frame(sid: str, hist: pd.DataFrame, weather: pd.DataFrame | None) -> pd.DataFrame:
    """All horizon-independent features for one dam, indexed by issue date."""
    raw = hist["inflow"]
    q = fill_short_gaps(raw)
    lq = log1(q)
    lvl = fill_short_gaps(hist["level"])
    f = pd.DataFrame(index=hist.index)
    f["station"] = STATION_CODE[sid]
    f["q0"] = q
    f["log_q0"] = lq
    f["observed0"] = hist["observed"].astype(int)
    for k in (1, 3, 7, 14):
        f[f"d{k}"] = lq - lq.shift(k)
    for k in (3, 7, 30):
        f[f"rm{k}"] = lq.rolling(k, min_periods=max(2, k // 2)).mean() - lq
    f["n_obs14"] = hist["observed"].astype(int).rolling(14, min_periods=1).sum()
    f["level0"] = lvl
    f["dl1"] = lvl - lvl.shift(1)
    f["dl7"] = lvl - lvl.shift(7)
    doy = hist.index.day_of_year
    f["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
    f["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)

    if weather is not None:
        w = weather.reindex(hist.index)
        past = w.shift(1)  # up to t-1
        for k in (3, 7, 14):
            f[f"pdd{k}"] = past["pdd"].rolling(k, min_periods=k).sum()
        f["tmax3"] = past["tmax"].rolling(3, min_periods=3).mean()
        f["tmean7"] = past["tmean"].rolling(7, min_periods=7).mean()
        f["dtemp7"] = f["tmean7"] - f["tmean7"].shift(7)
        for k in (3, 7, 14):
            f[f"precip{k}"] = past["precip"].rolling(k, min_periods=k).sum()
        f["snow7"] = past["snow"].rolling(7, min_periods=7).sum()
        f["snow30"] = past["snow"].rolling(30, min_periods=30).sum()
        f["_pdd"] = w["pdd"]
        f["_precip"] = w["precip"]
    else:
        for c in WEATHER_FEATURES:
            f[c] = np.nan
        f["_pdd"] = f["_precip"] = np.nan
    f["_raw"] = raw
    return f


def horizon_rows(frames: dict[str, pd.DataFrame], h: int, future_weather: bool = False) -> pd.DataFrame:
    """Rows usable for horizon h: today's flow known, target observed."""
    parts = []
    for sid, f in frames.items():
        d = f.copy()
        d["sid"] = sid
        d["issue_date"] = d.index
        d["target_date"] = d.index + pd.Timedelta(days=h)
        d["q_target"] = f["_raw"].shift(-h)
        d["z"] = log1(d["q_target"]) - d["log_q0"]
        if future_weather:
            d["pdd_fut"] = f["_pdd"].rolling(h, min_periods=h).sum().shift(-(h - 1))
            d["precip_fut"] = f["_precip"].rolling(h, min_periods=h).sum().shift(-(h - 1))
        parts.append(d)
    out = pd.concat(parts, ignore_index=True)
    return out[out["log_q0"].notna()]


def latest_rows(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """One row per dam at its newest date with a flow value (for live forecasts)."""
    parts = []
    for sid, f in frames.items():
        last = f[f["log_q0"].notna()].index.max()
        d = f.loc[[last]].copy()
        d["sid"] = sid
        d["issue_date"] = last
        parts.append(d)
    return pd.concat(parts, ignore_index=True)
