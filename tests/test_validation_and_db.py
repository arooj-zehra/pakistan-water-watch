from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DatabaseError, IntegrityError

from conftest import FIXTURES
from pipeline import db
from pipeline.backfill import load_pmd_files
from pipeline.validate import validate_rows


def obs(**kw):
    base = {"date": date(2024, 6, 1), "station_id": "tarbela", "level_ft": 1450.0,
            "inflow_cusecs": 150000.0, "outflow_cusecs": 120000.0, "source": "test"}
    return base | kw


@pytest.mark.parametrize("bad,reason", [
    (obs(inflow_cusecs=-5), "flow"),
    (obs(inflow_cusecs=15_000_000), "flow"),               # extra zero
    (obs(inflow_cusecs=224), "below plausible minimum"),   # "224, 700" misread
    (obs(level_ft=1193.0), "level"),                        # Mangla value in Tarbela column
    (obs(station_id="mangla", level_ft=1499.5), "level"),  # swapped dams
    (obs(station_id="warsak"), "unknown station"),
    (obs(date=date.today() + timedelta(days=5)), "future"),
    (obs(level_ft=None, inflow_cusecs=None, outflow_cusecs=None), "no level"),
])
def test_hard_rules_quarantine(bad, reason):
    good, quarantined = validate_rows([bad])
    assert good == [] and len(quarantined) == 1
    assert reason in quarantined[0][1]


def test_conflicting_duplicates_and_identical_duplicates():
    good, bad = validate_rows([obs(), obs()])
    assert len(good) == 1 and bad == []
    good, bad = validate_rows([obs(), obs(inflow_cusecs=151000.0)])
    assert len(good) == 1 and "conflicting duplicate" in bad[0][1]


def test_level_jump():
    rows = [obs(date=date(2024, 6, 1), level_ft=1450.0), obs(date=date(2024, 6, 2), level_ft=1490.0)]
    good, bad = validate_rows(rows)
    assert len(good) == 1 and "jumped" in bad[0][1]


def test_below_dead_level_is_soft_flag():
    good, bad = validate_rows([obs(level_ft=1395.0)], dead_level_lookup=lambda s, d: 1402.0)
    assert bad == [] and good[0]["flags"] == "below_dead_level"


def test_upsert_is_idempotent_and_keeps_known_values(engine):
    with engine.begin() as c:
        db.upsert(c, db.observations, [obs(live_storage_maf=2.5, fetched_at=db.utcnow())], ["date", "station_id"])
        # second source has no storage and a corrected inflow
        db.upsert(c, db.observations, [obs(inflow_cusecs=151000.0, live_storage_maf=None, fetched_at=db.utcnow())],
                  ["date", "station_id"])
        rows = c.execute(select(db.observations)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["inflow_cusecs"] == 151000.0 and rows[0]["live_storage_maf"] == 2.5


def test_forecasts_are_immutable(engine):
    row = {"issued_on": date(2026, 11, 1), "station_id": "tarbela", "horizon_days": 3,
           "model_version": "v0", "target_date": date(2026, 11, 4), "p05": 1, "p50": 2, "p95": 3,
           "created_at": db.utcnow()}
    with engine.begin() as c:
        c.execute(db.forecasts.insert().values(**row))
    with pytest.raises((DatabaseError, IntegrityError)):
        with engine.begin() as c:
            c.execute(db.forecasts.update().values(p50=99))
    with pytest.raises((DatabaseError, IntegrityError)):
        with engine.begin() as c:
            c.execute(db.forecasts.delete())


def test_dead_level_lookup_by_date(engine):
    with engine.connect() as c:
        assert db.dead_level_on(c, "tarbela", date(2026, 1, 1)) == 1402.0
        assert db.dead_level_on(c, "tarbela", date(2010, 1, 1)) == 1369.0
        assert db.dead_level_on(c, "tarbela", date(1990, 1, 1)) is None


def test_backfill_from_fixture_sheets(engine, tmp_path):
    for name, target in (("pmd_dec15.htm", "damsdec15.htm"), ("pmd_jun22_synthetic.htm", "damsjun22.htm")):
        (tmp_path / target).write_bytes((FIXTURES / name).read_bytes())
    with engine.begin() as c:
        ok, bad = load_pmd_files(c, list(tmp_path.glob("*.htm")))
        n_obs = len(c.execute(select(db.observations)).all())
        n_q = len(c.execute(select(db.quarantine)).all())
    assert ok == n_obs == 2 * 2 + 30 * 2  # Dec15: 2 days x 2 dams; Jun22: 30 x 2
    assert bad == n_q == 1                 # the '1O5000' typo note
    # running again changes nothing
    with engine.begin() as c:
        load_pmd_files(c, list(tmp_path.glob("*.htm")))
        assert len(c.execute(select(db.observations)).all()) == n_obs
