"""Phase 3: features without look-ahead, honest folds, calibration, artifacts, live forecasts."""
import csv
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("lightgbm")

from pipeline import config, fetch_weather, http  # noqa: E402
from pipeline.sources import openmeteo  # noqa: E402
from models import backtest, data, features, predict, train  # noqa: E402

START, END = "2017-01-01", "2026-10-30"


def synthetic(tmp_path, gap_days=()):
    """Snowmelt-like flows driven by lagged warmth, plus ERA5 CSVs for every point."""
    rng = np.random.default_rng(0)
    idx = pd.date_range(START, END, freq="D")
    doy = idx.day_of_year.to_numpy()
    temp = 12 * np.sin(2 * np.pi * (doy - 110) / 365.25) + rng.normal(0, 2, len(idx))
    pdd = pd.Series(np.maximum(temp, 0), index=idx).rolling(10, min_periods=1).mean().to_numpy()
    stations = {}
    for sid, base, scale in (("tarbela", 25000, 9000), ("mangla", 8000, 3000)):
        q = base + scale * pdd + rng.normal(0, base * 0.03, len(idx))
        inflow = [None if (d in gap_days or rng.random() < 0.05) else round(float(v)) for d, v in zip(idx, q)]
        level = (1450 + np.cumsum(rng.normal(0, 0.3, len(idx)))).round(2).tolist()
        stations[sid] = {"date": [str(d.date()) for d in idx], "inflow_cusecs": inflow, "level_ft": level}
    hist = tmp_path / "history.json"
    hist.write_text(json.dumps({"stations": stations}))
    wdir = tmp_path / "weather"
    wdir.mkdir()
    for p in config.WEATHER_POINTS:
        with (wdir / f"era5_{p.id}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(fetch_weather.CSV_FIELDS)
            for d, t in zip(idx[:-4], temp[:-4]):  # ERA5 lags a few days
                w.writerow([d.date(), p.id, round(t + 4, 1), round(t - 4, 1), round(t, 1),
                            round(max(0, rng.normal(1, 2)), 1), 0.0, "era5"])
    return hist, wdir


def frames_for(hist, wdir, recent=False):
    h = data.load_history(hist)
    w = data.load_weather(wdir, recent=recent)
    return {sid: features.station_frame(sid, df, w.get(sid)) for sid, df in h.items()}


def test_weather_features_use_only_days_before_issue(tmp_path):
    hist, wdir = synthetic(tmp_path)
    h = data.load_history(hist)["tarbela"]
    w = data.load_weather(wdir)["tarbela"]
    day = pd.Timestamp("2024-06-15")
    a = features.station_frame("tarbela", h, w)
    w2 = w.copy()
    w2.loc[day:, ["tmean", "tmax", "precip", "snow", "pdd"]] += 50  # change the issue day and after
    b = features.station_frame("tarbela", h, w2)
    cols = features.WEATHER_FEATURES
    pd.testing.assert_frame_equal(a.loc[:day, cols], b.loc[:day, cols])
    assert (a.loc[day + pd.Timedelta(days=1), "pdd3"] != b.loc[day + pd.Timedelta(days=1), "pdd3"])


def test_targets_are_observed_values_only(tmp_path):
    gap = pd.Timestamp("2023-05-10")
    hist, wdir = synthetic(tmp_path, gap_days={gap})
    rows = features.horizon_rows(frames_for(hist, wdir), 1)
    r = rows[(rows["sid"] == "tarbela") & (rows["target_date"] == gap)]
    assert len(r) == 1 and np.isnan(r["z"].iloc[0])  # gap is filled for features, never as a target
    t = rows[(rows["sid"] == "tarbela") & (rows["issue_date"] == gap)]
    assert t["log_q0"].notna().all()


def test_folds_never_train_on_the_test_year(tmp_path):
    hist, wdir = synthetic(tmp_path)
    rows = features.horizon_rows(frames_for(hist, wdir), 7)
    tr, cal, te = backtest.split(rows, 2023)
    assert tr["target_date"].max() < pd.Timestamp("2022-01-01")
    assert cal["target_date"].min() >= pd.Timestamp("2022-01-01") and cal["target_date"].max() < pd.Timestamp("2023-01-01")
    assert te["issue_date"].dt.year.eq(2023).all()
    assert te["z"].notna().all()


def test_conformal_intervals_reach_nominal_coverage():
    rng = np.random.default_rng(1)
    n = 3000
    X = pd.DataFrame({"a": rng.normal(size=n), "b": rng.normal(size=n)})
    X["z"] = 0.5 * X["a"] + rng.standard_t(3, n) * 0.3  # heavy tails: raw quantiles under-cover
    hm = train.fit(X.iloc[:1500], X.iloc[1500:2200], 1, ["a", "b"])
    p = hm.predict(X.iloc[2200:])
    z = X["z"].iloc[2200:]
    for lvl in (50, 80, 90):
        cov = ((z >= p[f"zlo_{lvl}"]) & (z <= p[f"zhi_{lvl}"])).mean()
        assert abs(cov - lvl / 100) < 0.06, (lvl, cov)


def test_bias_correction_maps_recent_weather_onto_era5():
    idx = pd.date_range("2026-09-01", periods=40, freq="D")
    era5 = pd.DataFrame({"tmax_c": 10.0, "tmin_c": 0.0, "tmean_c": 5.0, "precip_mm": 2.0, "snowfall_cm": 1.0}, index=idx[:35])
    rec = pd.DataFrame({"tmax_c": 13.0, "tmin_c": 3.0, "tmean_c": 8.0, "precip_mm": 4.0, "snowfall_cm": 1.0}, index=idx)
    out, adj = data.bias_correct(era5, rec)
    assert out["tmean_c"].iloc[-1] == pytest.approx(5.0) and adj["tmean_c"] == -3.0
    assert out["precip_mm"].iloc[-1] == pytest.approx(2.0) and adj["precip_mm"] == 0.5
    short, adj2 = data.bias_correct(era5.iloc[:5], rec)
    assert adj2 == {"overlap_days": 5} and short["tmean_c"].iloc[-1] == 8.0


def add_recent(wdir):
    era5_end = pd.Timestamp(END) - pd.Timedelta(days=4)
    days = pd.date_range(era5_end - pd.Timedelta(days=29), pd.Timestamp(END) + pd.Timedelta(days=5))
    for p in config.WEATHER_POINTS:
        with (wdir / f"recent_{p.id}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(fetch_weather.RECENT_FIELDS)
            for d in days:
                kind = "past" if d <= pd.Timestamp(END) else "forecast"
                w.writerow([d.date(), p.id, kind, 30, 20, 25, 0, 0, "x"])
    return era5_end


def test_recent_weather_fills_only_after_era5(tmp_path):
    hist, wdir = synthetic(tmp_path)
    era5_end = add_recent(wdir)
    plain = data.load_weather(wdir)["tarbela"]
    topped = data.load_weather(wdir, recent=True)["tarbela"]
    assert plain.index.max() == era5_end
    assert topped.index.max() == pd.Timestamp(END)  # forecast days are not history
    pd.testing.assert_series_equal(plain["tmean"], topped["tmean"].loc[:era5_end], check_freq=False)


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("prod")
    hist, wdir = synthetic(tmp)
    frames = frames_for(hist, wdir)
    summary = {"live": {}}
    meta = backtest.train_production(frames, (1, 3), "vtest", tmp / "artifacts" / "vtest", summary)
    return tmp, hist, wdir, frames, meta


def test_artifacts_are_gzipped_and_versioned(production):
    tmp, *_ , meta = production
    art = tmp / "artifacts"
    assert (art / "LATEST").read_text().strip() == "vtest"
    files = sorted(p.name for p in (art / "vtest").glob("*.txt.gz"))
    assert len(files) == 2 * len(train.QUANTILES) and "h1_q50.txt.gz" in files
    assert set(meta["horizons"]) == {"1", "3"} and meta["features"] == features.feature_names(False)
    assert predict.latest_version(art) == art / "vtest"


def test_live_forecast_is_monotone_and_explained(production):
    tmp, hist, wdir, frames, meta = production
    prod = predict.Production(tmp / "artifacts" / "vtest")
    no_recent = predict.forecast(frames, prod, today=date(2026, 10, 31))
    assert any("weather features are carried forward" in w for w in no_recent["tarbela"]["warnings"])
    add_recent(wdir)
    out = predict.forecast(frames_for(hist, wdir, recent=True), prod, today=date(2026, 10, 31))
    for sid in ("tarbela", "mangla"):
        s = out[sid]
        assert s["issue_date"] == END and s["warnings"] == []
        for f in s["forecasts"]:
            qs = [f[k] for k in ("p05", "p10", "p25", "p50", "p75", "p90", "p95")]
            assert qs == sorted(qs) and qs[0] > 0
            assert len(f["drivers"]) <= 3 and f["explanation"].startswith(("Biggest influences: ", "No single factor"))
            assert all(d["feature"] not in ("doy_sin", "doy_cos", "station") for d in f["drivers"])
    assert out["tarbela"]["forecasts"][1]["target_date"] == "2026-11-02"


def test_weather_too_old_to_carry_is_flagged(production):
    tmp, hist, wdir, frames, meta = production
    prod = predict.Production(tmp / "artifacts" / "vtest")
    old = {sid: f.copy() for sid, f in frames.items()}
    for f in old.values():
        f.loc[f.index > pd.Timestamp("2026-10-15"), features.WEATHER_FEATURES] = np.nan
    out = predict.forecast(old, prod, today=date(2026, 10, 31))
    assert any("Weather is incomplete" in w for w in out["tarbela"]["warnings"])
    for f in out["tarbela"]["forecasts"]:
        assert all(not d["feature"].startswith(("pdd", "precip", "snow", "tmax", "tmean", "dtemp")) for d in f["drivers"])


def test_stale_data_is_flagged(production):
    tmp, hist, wdir, frames, meta = production
    prod = predict.Production(tmp / "artifacts" / "vtest")
    out = predict.forecast(frames, prod, today=date(2026, 11, 9))
    assert any("10 days old" in w for w in out["mangla"]["warnings"])


def test_era5_update_appends_new_days(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_weather, "WEATHER_DIR", tmp_path)
    p = config.WEATHER_POINTS[0]
    (tmp_path / f"era5_{p.id}.csv").write_text(",".join(fetch_weather.CSV_FIELDS) + f"\n2026-10-20,{p.id},1,0,0.5,0,0,era5\n")
    for q in config.WEATHER_POINTS[1:]:
        (tmp_path / f"era5_{q.id}.csv").write_text(",".join(fetch_weather.CSV_FIELDS) + f"\n2026-10-26,{q.id},1,0,0.5,0,0,era5\n")
    asked = []

    def fake(point, start, end):
        asked.append((point.id, start, end))
        return [{"date": date(2026, 10, d), "point_id": point.id, "tmax_c": 1, "tmin_c": 0, "tmean_c": 0.5,
                 "precip_mm": 0, "snowfall_cm": 0, "model": "era5"} for d in range(start.day, end.day + 1)]

    assert fetch_weather.update(date(2026, 10, 26), fetch=fake) == 0
    assert asked == [(p.id, date(2026, 10, 21), date(2026, 10, 26))]
    assert fetch_weather.last_date(tmp_path / f"era5_{p.id}.csv") == date(2026, 10, 26)


def test_recent_fetch_marks_past_and_forecast_days(monkeypatch):
    seen = {}
    today = date.today()

    def fake_get(url, **kw):
        seen.update(kw["params"])
        days = [str(today.fromordinal(today.toordinal() + k)) for k in (-1, 0)]
        return type("R", (), {"status_code": 200, "json": lambda self: {
            "daily": {"time": days, **{v: [1.0, 2.0] for v in config.WEATHER_DAILY_VARS}}}})()

    monkeypatch.setattr(http, "get", fake_get)
    rows = openmeteo.fetch_forecast(config.WEATHER_POINTS[0], days=16, past_days=60)
    assert seen["past_days"] == 60 and seen["forecast_days"] == 16
    assert [r["kind"] for r in rows] == ["past", "forecast"]
