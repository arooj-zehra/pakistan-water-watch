"""Phase 5: anomaly checks, the review decisions, and the terminal review tool."""
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("statsmodels")
pytest.importorskip("sklearn")

from pipeline import anomaly as A  # noqa: E402
from pipeline import db, queries, review  # noqa: E402


def synthetic(seed=0, years=3):
    """Two dams with seasonal inflow, operator outflows and a level that obeys the water balance."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=365 * years, freq="D")
    doy = idx.day_of_year.to_numpy()
    frames = {}
    for sid, base, amp, lvl0 in (("tarbela", 30000, 180000, 1480.0), ("mangla", 8000, 50000, 1150.0)):
        season = np.exp(-((doy - 200) / 45.0) ** 2)
        qin = base + amp * season * (1 + 0.05 * rng.standard_normal(len(idx)))
        qout = base + amp * np.exp(-((doy - 215) / 50.0) ** 2) * 0.95 + rng.normal(0, base * 0.03, len(idx))
        level = lvl0 + np.cumsum(2e-5 * (qin - qout)) + rng.normal(0, 0.05, len(idx))
        f = pd.DataFrame({"inflow_cusecs": qin.round(), "outflow_cusecs": qout.round(), "level_ft": level.round(2)}, idx)
        f[rng.random((len(idx), 3)) < 0.1] = np.nan  # some unreported days
        frames[sid] = f
    return frames


# Checks -----------------------------------------------------------------------

def test_trend_is_not_mistaken_for_an_error():
    idx = pd.date_range("2024-05-01", periods=40)
    rising = pd.Series(1400 + 1.2 * np.arange(40), idx)  # a lake filling 1.2 ft a day
    z, exp = A.neighbour_z(rising, "level_ft")
    assert np.nanmax(np.abs(z.iloc[1:-1])) < 0.5  # interior days sit on the trend line
    assert np.nanmax(np.abs(z)) < 3              # the end days have neighbours on one side only
    spiked = rising.copy()
    spiked.iloc[20] += 9  # 1424 -> 1433: a level typo
    z, exp = A.neighbour_z(spiked, "level_ft")
    assert abs(z.iloc[20]) > 10 and exp.iloc[20] == pytest.approx(rising.iloc[20], abs=0.5)


def test_water_balance_catches_swapped_flows():
    f = synthetic()["tarbela"]
    d = f.dropna().index[400]
    f.loc[d, ["inflow_cusecs", "outflow_cusecs"]] = f.loc[d, ["outflow_cusecs", "inflow_cusecs"]].to_numpy()
    before = A.balance_z(synthetic()["tarbela"])
    after = A.balance_z(f)
    if abs(f.at[d, "inflow_cusecs"] - f.at[d, "outflow_cusecs"]) > 20000:
        assert abs(after[d]) > A.BALANCE_Z > abs(before[d])


def test_clean_history_raises_few_flags():
    frames = synthetic()
    days = sum(f.notna().any(axis=1).sum() for f in frames.values())
    assert len(A.detect(frames)) / days < 0.01


def test_planted_errors_are_caught():
    frames = synthetic(seed=1)
    clean = {k: f.copy() for k, f in frames.items()}
    errors = []
    t, m = frames["tarbela"], frames["mangla"]
    obs = lambda f, col: f[col].dropna().index  # noqa: E731
    picks = iter(obs(t, "inflow_cusecs")[30::45])
    for mult in (10, 0.1, 1.6, 0.6, 10, 0.1):  # digit doubled / dropped, wrong digit / swapped digits
        d = next(picks)
        t.at[d, "inflow_cusecs"] = round(t.at[d, "inflow_cusecs"] * mult)
        errors.append(("tarbela", d, "inflow_cusecs"))
    for d in obs(m, "level_ft")[50::120][:5]:  # level typos of 9-10 ft
        m.at[d, "level_ft"] += 9.0
        errors.append(("mangla", d, "level_ft"))
    for d in [x for x in obs(m, "inflow_cusecs")[60::100] if not np.isnan(clean["tarbela"]["inflow_cusecs"].get(x, np.nan))][:4]:
        m.at[d, "inflow_cusecs"] = clean["tarbela"].at[d, "inflow_cusecs"]  # the other dam's inflow
        errors.append(("mangla", d, "inflow_cusecs"))
    keys = {fl.key for fl in A.detect(frames)}
    caught = [e for e in errors if (e[0], str(e[1].date()), e[2]) in keys]
    assert len(caught) >= 0.85 * len(errors), f"caught {len(caught)} of {len(errors)}"


# Decisions --------------------------------------------------------------------

def test_corrections_newest_decision_wins():
    dec = [{"station_id": "tarbela", "date": "2026-10-01", "field": "inflow_cusecs", "decision": "bad"},
           {"station_id": "tarbela", "date": "2026-10-02", "field": "level_ft", "decision": "set", "value": "1500.5"},
           {"station_id": "tarbela", "date": "2026-10-01", "field": "inflow_cusecs", "decision": "ok"},
           {"station_id": "mangla", "date": "2026-10-03", "field": "inflow_cusecs", "decision": "bad"}]
    assert A.corrections(dec) == {("tarbela", "2026-10-02", "level_ft"): 1500.5,
                                  ("mangla", "2026-10-03", "inflow_cusecs"): None}
    rows = [{"date": date(2026, 10, 2), "station_id": "tarbela", "level_ft": 1400.0, "inflow_cusecs": 1.0},
            {"date": date(2026, 10, 3), "station_id": "mangla", "level_ft": 1100.0, "inflow_cusecs": 99999.0}]
    dec.append({"station_id": "mangla", "date": "2026-10-05", "field": "inflow_cusecs", "decision": "set", "value": "7000"})
    out = A.apply_corrections(rows, dec)
    assert out[0]["inflow_cusecs"] is None and out[0]["level_ft"] == 1100.0  # mangla 10-03 dropped
    assert out[1]["inflow_cusecs"] == 7000 and out[1]["source"] == "review"  # added from a decision
    assert out[2]["level_ft"] == 1500.5
    assert rows[1]["inflow_cusecs"] == 99999.0  # originals untouched


def test_decisions_apply_wherever_data_is_read(engine):
    with engine.begin() as c:
        db.upsert(c, db.observations, [
            {"date": date(2026, 10, 29), "station_id": "mangla", "inflow_cusecs": 7000, "level_ft": 1195.0, "source": "app"},
            {"date": date(2026, 10, 30), "station_id": "mangla", "inflow_cusecs": 70000, "level_ft": 1195.5, "source": "app"},
        ], keys=["date", "station_id"])
    A.append_decisions([{"kind": "anomaly", "station_id": "mangla", "date": "2026-10-30", "field": "inflow_cusecs",
                         "decision": "set", "value": 7100, "decided_on": "2026-11-01"}])
    with engine.connect() as c:
        h = queries.history(c, "mangla")["stations"]["mangla"]
        lt = {s["id"]: s for s in queries.latest(c, today=date(2026, 10, 31))["stations"]}
    assert h["inflow_cusecs"] == [7000, 7100]
    assert lt["mangla"]["inflow_cusecs"] == 7100 and lt["mangla"]["previous"]["inflow_cusecs"] == 7000
    assert len(A.raw_rows(engine)) == 2 and {r["inflow_cusecs"] for r in A.raw_rows(engine)} == {7000, 70000}


def test_review_queue_skips_decided_items(engine, tmp_path):
    frames = synthetic()
    d = frames["tarbela"]["inflow_cusecs"].dropna().index[300]
    frames["tarbela"].at[d, "inflow_cusecs"] *= 10
    flags = A.detect(frames)
    key = ("tarbela", str(d.date()), "inflow_cusecs")
    assert key in {f.key for f in flags}
    conf = tmp_path / "conf.csv"
    conf.write_text("date,station_id,kept_source,kept_inflow,kept_link,other_source,other_inflow,other_link\n"
                    "2023-05-01,mangla,app,30000,u1,urdupoint,30500,u2\n"
                    "2023-05-02,mangla,app,30000,u1,urdupoint,40000,u2\n")
    conflicts = A.conflicts(conf)
    assert len(conflicts) == 1 and conflicts[0]["gap"] == 0.25
    q = [{"ref": "q1", "flagged_at": "2026-01-01", "source": "news", "reason": "level jumped",
          "parsed": {"station_id": "tarbela", "date": "2024-01-01", "level_ft": 1500.0}, "raw_text": None}]
    r = A.build_review(frames, flags, q, conflicts, [])
    assert r["counts"] == {"anomaly": len(flags), "quarantine": 1, "conflict": 1}
    item = next(p for p in r["pending"] if p["kind"] == "anomaly" and (p["station_id"], p["date"], p["field"]) == key)
    assert item["context"]["dates"][15] == str(d.date()) and len(item["context"]["values"]) == 31
    decided = [{"station_id": key[0], "date": key[1], "field": key[2], "decision": "bad", "ref": ""},
               {"ref": "q1", "decision": "dismiss"}, {"ref": conflicts[0]["ref"], "decision": "ok"}]
    r2 = A.build_review(frames, flags, q, conflicts, decided)
    assert r2["counts"] == {"anomaly": len(flags) - 1, "quarantine": 0, "conflict": 0}
    json.dumps(r2, default=str)  # the website file must serialise


def test_terminal_review_saves_each_decision():
    items = [
        {"kind": "anomaly", "ref": "a1", "station_id": "mangla", "date": "2026-04-29", "field": "outflow_cusecs",
         "value": 40.0, "expected": 35000.0, "checks": ["local"], "context": None},
        {"kind": "anomaly", "ref": "a2", "station_id": "mangla", "date": "2026-04-27", "field": "outflow_cusecs",
         "value": 35.0, "expected": 35000.0, "checks": ["local"], "context": None},
        {"kind": "quarantine", "ref": "q3", "source": "news", "flagged_at": "2026-01-01", "reason": "level jumped +24 ft",
         "parsed": {"station_id": "tarbela", "date": "2017-07-27", "inflow_cusecs": 253500.0, "level_ft": 1550.0}},
        {"kind": "conflict", "ref": "c4", "station_id": "tarbela", "date": "2020-01-01", "field": "inflow_cusecs",
         "kept": 20000.0, "kept_source": "app", "kept_link": "x", "other": 26000.0, "other_source": "urdupoint",
         "other_link": "y", "gap": 0.23},
        {"kind": "anomaly", "ref": "a5", "station_id": "tarbela", "date": "2026-01-01", "field": "level_ft",
         "value": 1.0, "expected": 2.0, "checks": [], "context": None},
    ]
    answers = iter(["b", "x", "s", "35,000", "a", "u", "q"])
    saved = review.run(items, read=lambda prompt: next(answers))
    rows = A.read_decisions()
    assert saved == len(rows) == 5
    assert [r["decision"] for r in rows] == ["bad", "set", "set", "set", "set"]
    assert rows[1]["value"] == "35000.0" and rows[1]["note"] == "was 35.0"
    assert {r["field"] for r in rows[2:4]} == {"inflow_cusecs", "level_ft"}  # quarantine accepted in full
    assert rows[4]["value"] == "26000.0"
