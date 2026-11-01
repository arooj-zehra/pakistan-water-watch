"""Phase 4: forecasts issued once, scored honestly, monitored, and promoted with a check."""
import csv
import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import inspect, text

pytest.importorskip("lightgbm")

from pipeline import db  # noqa: E402
from models import backtest, daily, retrain  # noqa: E402
from test_models import END, add_recent, frames_for, synthetic  # noqa: E402

TODAY = date(2026, 10, 31)


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("live")
    hist, wdir = synthetic(tmp)
    add_recent(wdir)
    backtest.train_production(frames_for(hist, wdir), (1, 3), "vtest", tmp / "artifacts" / "vtest", {"live": {}})
    return tmp, hist, wdir


def fresh_engine(tmp_path):
    eng = db.get_engine(f"sqlite:///{(tmp_path / 'pww.db').as_posix()}")
    db.init_db(eng)
    return eng


def run(setup, tmp_path, eng, today=TODAY):
    tmp, hist, wdir = setup
    return daily.run(eng, today=today, artifacts=tmp / "artifacts" / "vtest", history=hist, weather_dir=wdir,
                     out_dir=tmp_path / "out", log_csv=tmp_path / "issued.csv")


def test_migration_adds_columns_to_an_existing_table(tmp_path):
    eng = db.get_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE forecasts (issued_on DATE, station_id VARCHAR(32), horizon_days INTEGER, "
                       "model_version VARCHAR(64), target_date DATE NOT NULL, p05 FLOAT, p50 FLOAT, p95 FLOAT, "
                       "created_at DATETIME NOT NULL, PRIMARY KEY (issued_on, station_id, horizon_days, model_version))"))
        c.execute(text("INSERT INTO forecasts VALUES ('2026-10-01','tarbela',1,'v1','2026-10-02',1,2,3,'2026-10-01')"))
    assert db.migrate(eng) == list(db.FORECAST_ADDED_COLUMNS)
    cols = {c["name"] for c in inspect(eng).get_columns("forecasts")}
    assert set(db.FORECAST_ADDED_COLUMNS) <= cols
    assert db.migrate(eng) == []
    with eng.connect() as c:
        assert c.execute(text("SELECT p50 FROM forecasts")).scalar() == 2


def test_forecasts_are_issued_once_and_logged(setup, tmp_path):
    eng = fresh_engine(tmp_path)
    assert run(setup, tmp_path, eng) == 0
    with eng.connect() as c:
        n = c.execute(text("SELECT COUNT(*) FROM forecasts")).scalar()
    assert n == 2 * 2  # two dams x horizons 1 and 3
    run(setup, tmp_path, eng)  # same data, same day: nothing new
    with eng.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM forecasts")).scalar() == n
    rows = list(csv.DictReader((tmp_path / "issued.csv").open()))
    assert len(rows) == n and rows[0]["issued_on"] == END and rows[0]["model_version"] == "vtest"
    fc = json.loads((tmp_path / "out" / "forecast.json").read_text())
    assert fc["stations"]["tarbela"]["warnings"] == []
    acc = json.loads((tmp_path / "out" / "accuracy.json").read_text())
    assert acc["forecasts_issued"] == n and acc["scored"] == 0 and acc["first_issued"] == END
    with pytest.raises(Exception, match="immutable"), eng.begin() as c:
        c.execute(text("UPDATE forecasts SET p50 = 0"))


def test_forecasts_are_scored_against_reported_inflow(setup, tmp_path):
    eng = fresh_engine(tmp_path)
    run(setup, tmp_path, eng)
    with eng.connect() as c:
        fc = pd.read_sql(text("SELECT * FROM forecasts"), c)
    obs = [{"date": pd.Timestamp(r.target_date).date(), "station_id": r.station_id, "inflow_cusecs": r.p50 * 1.01,
            "source": "test"} for r in fc.itertuples()]
    with eng.begin() as c:
        db.upsert(c, db.observations, obs, keys=["date", "station_id"])
    run(setup, tmp_path, eng, today=date(2026, 11, 4))
    acc = json.loads((tmp_path / "out" / "accuracy.json").read_text())
    assert acc["scored"] == len(fc)
    t = acc["by_horizon"]["1"]
    assert t["n"] == 2 and t["coverage90"] == 1.0
    expected = np.mean([abs(r.p50 * 1.01 - r.inflow_at_issue) for r in fc.itertuples() if r.horizon_days == 1])
    assert t["mae_persistence"] == round(expected)
    assert {r["h"] for r in acc["recent"]} == {1, 3} and all(r["actual"] for r in acc["recent"])


def test_a_day_issued_twice_counts_once(tmp_path):
    eng = fresh_engine(tmp_path)
    base = {"issued_on": date(2026, 10, 1), "station_id": "mangla", "horizon_days": 1, "target_date": date(2026, 10, 2),
            "p05": 1, "p10": 2, "p25": 3, "p50": 100, "p75": 5, "p90": 6, "p95": 200, "inflow_at_issue": 90}
    with eng.begin() as c:
        db.insert_new(c, db.forecasts, [{**base, "model_version": "v1", "created_at": datetime(2026, 10, 1, 5)},
                                        {**base, "model_version": "v2", "p50": 999, "created_at": datetime(2026, 10, 1, 9)}],
                      ["issued_on", "station_id", "horizon_days", "model_version"])
        db.upsert(c, db.observations, [{"date": date(2026, 10, 2), "station_id": "mangla", "inflow_cusecs": 110,
                                        "source": "test"}], keys=["date", "station_id"])
    s = daily.scored_forecasts(eng)
    assert len(s) == 1 and s["model_version"].iloc[0] == "v1"
    assert daily.score_table(s)["mae_model"] == 10


def test_monitor_alerts_on_poor_coverage_and_drift():
    rng = np.random.default_rng(0)
    n = 40
    dates = pd.date_range("2026-09-15", periods=n)
    s = pd.DataFrame({"horizon_days": 5, "target_date": dates, "actual": 1000.0, "p50": 1500.0, "inflow_at_issue": 1100.0,
                      **{q: v for q, v in zip(daily.QCOLS, (1400, 1420, 1450, 1500, 1550, 1580, 1600))}})
    m = daily.monitor(s, {}, date(2026, 11, 1))
    assert m["status"] == "alert" and len(m["alerts"]) == 2  # coverage 0% and worse than persistence
    ref = rng.normal(0, 1, 2000)
    assert daily.psi(ref, rng.normal(0, 1, 60)) < 0.25
    assert daily.psi(ref, rng.normal(2, 1, 60)) > 0.25
    assert daily.psi(ref, np.array([np.nan] * 60)) is None
    d = {"tarbela": {"pdd7": {"psi": 0.9, "past_years": 8, "past_max": 0.6, "past_p75": 0.4},    # beyond any past year
                     "log_q0": {"psi": 0.5, "past_years": 8, "past_max": 0.6, "past_p75": 0.4},  # unusual, not unprecedented
                     "d7": {"psi": 0.3, "past_years": 8, "past_max": 1.5, "past_p75": 0.5},      # ordinary
                     "precip7": {"psi": 0.9, "past_years": 2, "past_max": 0.1, "past_p75": 0.1}}}  # too little history
    m2 = daily.monitor(s.iloc[:10], d, date(2026, 11, 1))
    assert m2["status"] == "alert" and len(m2["alerts"]) == 1 and "pdd7" in m2["alerts"][0]
    assert m2["watch"] == ["tarbela: log_q0 PSI 0.50 (top quarter of 8 past years)"]


def test_drift_is_judged_against_past_years(setup):
    tmp, hist, wdir = setup
    frames = frames_for(hist, wdir)  # ERA5 only: the fake recent weather is a constant 25 C
    out = daily.drift(frames, "2025-10-30")
    d = out["tarbela"]["pdd7"]
    assert d["past_years"] >= 6 and d["psi"] is not None and d["past_max"] >= d["past_p75"]
    m = daily.monitor(pd.DataFrame(), out, date(2026, 10, 31))
    assert m["alerts"] == []  # synthetic weather has no regime change


def test_same_season_wraps_over_new_year():
    idx = pd.to_datetime(["2020-12-20", "2021-01-05", "2021-06-01"])
    assert list(daily.same_season(idx, pd.Timestamp("2025-12-01"), pd.Timestamp("2026-01-29"))) == [True, True, False]


def meta(version, skills, cov=0.9):
    return {"version": version, "horizons": {str(h): {"backtest": {"skill_vs_persistence": s},
                                                      "holdout": {"coverage90": cov}} for h, s in zip((1, 3, 5), skills)}}


def test_promotion_rules():
    cur = meta("v1", (0.2, 0.2, 0.2))
    assert retrain.decide(meta("v2", (0.2, 0.21, 0.19)), cur)[0]
    assert not retrain.decide(meta("v2", (0.1, 0.1, 0.1)), cur)[0]           # clearly worse
    assert retrain.decide(meta("v2", (0.1, 0.1, 0.1)), cur, force=True)[0]   # unless forced
    ok, why = retrain.decide(meta("v2", (0.3, -0.01, 0.3)), cur, force=True)
    assert not ok and "3-day" in why[0]                                      # never promote a failing model
    assert not retrain.decide(meta("v2", (0.2, 0.2, 0.2), cov=0.7), cur)[0]
    assert not retrain.decide(meta("v1", (0.3, 0.3, 0.3)), cur)[0]           # nothing new
    assert retrain.decide(meta("v1", (0.2, 0.2, 0.2)), None)[0]
