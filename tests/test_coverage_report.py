from datetime import date, timedelta

import numpy as np
import pandas as pd

from pipeline import coverage_report as cr
from pipeline import db


def test_fill_short_gaps_fills_only_short_inside_runs():
    s = pd.Series([1.0, np.nan, np.nan, 4.0, np.nan, np.nan, np.nan, np.nan, 9.0, np.nan])
    out = cr.fill_short_gaps(s, max_gap=3)
    assert out[:4].tolist() == [1.0, 2.0, 3.0, 4.0]   # 2-day gap filled linearly
    assert out[4:8].isna().all()                       # 4-day gap left entirely empty
    assert np.isnan(out[9])                            # trailing gap: no value after it


def _load(engine, station, days, start=date(2021, 1, 1), skip=()):
    rows = [{"date": start + timedelta(days=i), "station_id": station, "inflow_cusecs": 1000.0 + i,
             "source": "app", "fetched_at": db.utcnow()} for i in range(days) if i not in skip]
    with engine.begin() as c:
        db.upsert(c, db.observations, rows, ["date", "station_id"])


def test_db_report_verdict_with_and_without_filling(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "DB_HEATMAP_PNG", tmp_path / "db.png")  # never touch docs/img
    # 2021-2025 complete except a 2-day gap every 10 days (20% missing raw)
    n = (date(2026, 1, 1) - date(2021, 1, 1)).days
    skip = {i for i in range(n) if i % 10 in (4, 5)}
    for sid in ("tarbela", "mangla"):
        _load(engine, sid, n, skip=skip)
    section, v_raw, v_fill, v_rev = cr.db_report(engine)
    assert not v_raw.passed and v_raw.good_years == []
    assert v_fill.passed and v_fill.good_years == [2021, 2022, 2023, 2024, 2025]
    assert "| app |" in section and "2021" in section
    assert (tmp_path / "db.png").exists()
    assert v_rev.passed and "GO (revised gate)" in section


def test_revised_gate_needs_both_parts():
    import pandas as pd
    def years(missing):  # {year: worst_missing}
        return pd.DataFrame({"months_probed": 12, "worst_missing": list(missing.values())}, index=list(missing))
    ok = cr.revised_verdict(years({2020: 0.21, 2021: 0.08, 2022: 0.43, 2023: 0.04, 2024: 0.25, 2025: 0.25}))
    assert ok.passed and ok.good_years == [2021, 2023] and ok.best_run == [2020, 2021, 2023, 2024, 2025]
    assert not cr.revised_verdict(years({2021: 0.08, 2023: 0.04, 2024: 0.25})).passed      # too few training years
    assert not cr.revised_verdict(years({y: 0.2 for y in range(2019, 2026)})).passed       # no backtest years


def test_db_report_empty(engine):
    assert cr.db_report(engine) is None


def test_db_monthly_current_month_counts_days_so_far(engine):
    _load(engine, "tarbela", 10, start=date(2026, 11, 1))
    daily = cr.db_daily(engine)
    m = cr.db_monthly(daily, fill=False, today="2026-11-10")
    nov = m[m.month == "2026-11"].iloc[0]
    assert nov.days_in_month == 10 and nov.tarbela_days == 10 and nov.mangla_days == 0
