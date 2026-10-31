"""Database schema and helpers. Works on SQLite (local dev, tests) and
PostgreSQL (Neon/Supabase) through SQLAlchemy Core.

    python -m pipeline.db init     # create tables, triggers, seed stations

Design rules (from the project plan):
  * observations are keyed on (date, station_id) and written with upserts, so
    re-running any job never duplicates rows;
  * an upsert never replaces a known value with NULL;
  * forecasts are written once and a database trigger blocks UPDATE/DELETE,
    so live scores can't be quietly improved after the fact.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import (
    Column, Date, DateTime, Float, Integer, MetaData, String, Table, Text,
    create_engine, event, func, select, text,
)
from sqlalchemy.engine import Connection, Engine

from .config import DATABASE_URL, DEAD_LEVELS_SEED, STATIONS

metadata = MetaData()


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


stations = Table(
    "stations", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False),
    Column("river", String(64), nullable=False),
    Column("max_level_ft", Float, nullable=False),
)

dead_levels = Table(
    "dead_levels", metadata,
    Column("station_id", String(32), primary_key=True),
    Column("valid_from", Date, primary_key=True),
    Column("level_ft", Float, nullable=False),
    Column("source", String(64)),  # 'seed' or 'wapda_press'
)

observations = Table(
    "observations", metadata,
    Column("date", Date, primary_key=True),
    Column("station_id", String(32), primary_key=True),
    Column("level_ft", Float),
    Column("inflow_cusecs", Float),
    Column("outflow_cusecs", Float),
    Column("live_storage_maf", Float),
    Column("source", String(32), nullable=False),  # 'pmd' | 'wapda_press'
    Column("flags", String(256)),  # soft warnings, e.g. 'below_dead_level'
    Column("fetched_at", DateTime, nullable=False, default=utcnow),
)

# ERA5 reanalysis: what the weather actually was (lags real time ~5 days)
weather = Table(
    "weather", metadata,
    Column("date", Date, primary_key=True),
    Column("point_id", String(32), primary_key=True),
    Column("tmax_c", Float), Column("tmin_c", Float), Column("tmean_c", Float),
    Column("precip_mm", Float), Column("snowfall_cm", Float),
    Column("fetched_at", DateTime, nullable=False, default=utcnow),
)

# Weather forecasts as issued each day. Keeping every issue builds our own
# forecast archive, which is what lets the model train on the same kind of
# weather input it gets live (the look-ahead trap in the plan).
weather_forecast = Table(
    "weather_forecast", metadata,
    Column("issued_on", Date, primary_key=True),
    Column("date", Date, primary_key=True),
    Column("point_id", String(32), primary_key=True),
    Column("tmax_c", Float), Column("tmin_c", Float), Column("tmean_c", Float),
    Column("precip_mm", Float), Column("snowfall_cm", Float),
    Column("fetched_at", DateTime, nullable=False, default=utcnow),
)

forecasts = Table(
    "forecasts", metadata,
    Column("issued_on", Date, primary_key=True),
    Column("station_id", String(32), primary_key=True),
    Column("horizon_days", Integer, primary_key=True),
    Column("model_version", String(64), primary_key=True),
    Column("target_date", Date, nullable=False),
    Column("p05", Float), Column("p50", Float), Column("p95", Float),
    Column("created_at", DateTime, nullable=False, default=utcnow),
)

ingest_log = Table(
    "ingest_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_at", DateTime, nullable=False, default=utcnow),
    Column("source", String(32), nullable=False),
    Column("status", String(16), nullable=False),  # ok | partial | failed | stale
    Column("rows_added", Integer, nullable=False, default=0),
    Column("error", Text),
)

quarantine = Table(
    "quarantine", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("flagged_at", DateTime, nullable=False, default=utcnow),
    Column("source", String(32)),
    Column("raw_text", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("review_status", String(16), nullable=False, default="pending"),  # Phase 5 admin page
)

_IMMUTABLE_SQLITE = [
    """CREATE TRIGGER IF NOT EXISTS forecasts_no_update BEFORE UPDATE ON forecasts
       BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS forecasts_no_delete BEFORE DELETE ON forecasts
       BEGIN SELECT RAISE(ABORT, 'forecasts are immutable'); END""",
]
_IMMUTABLE_PG = [
    """CREATE OR REPLACE FUNCTION forecasts_immutable() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'forecasts are immutable'; END; $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS forecasts_no_change ON forecasts",
    """CREATE TRIGGER forecasts_no_change BEFORE UPDATE OR DELETE ON forecasts
       FOR EACH ROW EXECUTE FUNCTION forecasts_immutable()""",
]


def get_engine(url: str | None = None) -> Engine:
    url = url or DATABASE_URL
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        from pathlib import Path

        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(url, future=True)
    if eng.dialect.name == "sqlite":
        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    return eng


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)
    with engine.begin() as conn:
        for stmt in (_IMMUTABLE_PG if engine.dialect.name == "postgresql" else _IMMUTABLE_SQLITE):
            conn.execute(text(stmt))
        upsert(conn, stations, [
            {"id": s.id, "name": s.name, "river": s.river, "max_level_ft": s.max_level_ft}
            for s in STATIONS.values()
        ], keys=["id"], overwrite_nulls=True)
        upsert(conn, dead_levels, [
            {"station_id": sid, "valid_from": d, "level_ft": lvl, "source": "seed"}
            for sid, d, lvl in DEAD_LEVELS_SEED
        ], keys=["station_id", "valid_from"], do_nothing=True)


def _insert_for(conn: Connection):
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif conn.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover
        raise NotImplementedError(conn.dialect.name)
    return insert


def upsert(conn: Connection, table: Table, rows: Iterable[dict], keys: list[str],
           *, do_nothing: bool = False, overwrite_nulls: bool = False) -> int:
    """INSERT ... ON CONFLICT (keys) DO UPDATE. Returns rows submitted.

    By default an incoming NULL keeps the existing value (COALESCE), so a
    partial source never erases a complete one.
    """
    rows = list(rows)
    if not rows:
        return 0
    insert = _insert_for(conn)
    stmt = insert(table)
    if do_nothing:
        stmt = stmt.on_conflict_do_nothing(index_elements=keys)
    else:
        cols = {c for r in rows for c in r} - set(keys)
        set_ = {
            c: (stmt.excluded[c] if overwrite_nulls else func.coalesce(stmt.excluded[c], table.c[c]))
            for c in cols
        }
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=set_)
    # Chunk to stay under SQLite's bound-parameter limit
    for i in range(0, len(rows), 500):
        conn.execute(stmt, rows[i:i + 500])
    return len(rows)


def log_run(conn: Connection, source: str, status: str, rows_added: int = 0, error: str | None = None) -> None:
    conn.execute(ingest_log.insert().values(
        run_at=utcnow(), source=source, status=status, rows_added=rows_added, error=error))


def add_quarantine(conn: Connection, items: list[tuple[str, str]], source: str) -> int:
    """items: (raw_text, reason)"""
    if items:
        conn.execute(quarantine.insert(), [
            {"flagged_at": utcnow(), "source": source, "raw_text": raw, "reason": reason,
             "review_status": "pending"} for raw, reason in items
        ])
    return len(items)


def dead_level_on(conn: Connection, station_id: str, d) -> float | None:
    row = conn.execute(
        select(dead_levels.c.level_ft)
        .where(dead_levels.c.station_id == station_id, dead_levels.c.valid_from <= d)
        .order_by(dead_levels.c.valid_from.desc()).limit(1)
    ).first()
    return row[0] if row else None


def latest_observation_date(conn: Connection, station_id: str):
    return conn.execute(
        select(func.max(observations.c.date)).where(
            observations.c.station_id == station_id,
            observations.c.inflow_cusecs.is_not(None),
        )
    ).scalar()


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv[:1] != ["init"]:
        print("usage: python -m pipeline.db init")
        return 1
    eng = get_engine()
    init_db(eng)
    print(f"Initialised {eng.url.render_as_string(hide_password=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
