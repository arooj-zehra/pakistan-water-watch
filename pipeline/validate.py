"""Validation: rows that fail never reach `observations`; they go to quarantine.

Hard rules (quarantine):
  * level inside the station's physical bounds (catches typos, swapped dams)
  * flows non-negative and below a plausible ceiling (catches an extra zero)
  * inflow above a per-dam floor (catches "224, 700" read as 224)
  * date not in the future
  * no conflicting duplicates for the same (date, station) in one batch
  * day-to-day level jump no larger than MAX_LEVEL_JUMP_FT

Soft rules (row kept, `flags` set): level below that date's dead level.
Dead levels changed over time and the seed dates still need verifying, so this
is a warning, not a rejection.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Callable, Optional

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from .config import MAX_PLAUSIBLE_FLOW_CUSECS, MIN_PLAUSIBLE_INFLOW_CUSECS, STATIONS

MAX_LEVEL_JUMP_FT = 15.0  # Tarbela/Mangla rarely move more than a few ft/day


class Observation(BaseModel):
    date: date
    station_id: str
    level_ft: Optional[float] = None
    inflow_cusecs: Optional[float] = None
    outflow_cusecs: Optional[float] = None
    live_storage_maf: Optional[float] = None
    source: str

    @field_validator("station_id")
    @classmethod
    def known_station(cls, v: str) -> str:
        if v not in STATIONS:
            raise ValueError(f"unknown station {v!r}")
        return v

    @field_validator("date")
    @classmethod
    def not_future(cls, v: date) -> date:
        if v > date.today() + timedelta(days=1):
            raise ValueError(f"date {v} is in the future")
        return v

    @field_validator("inflow_cusecs", "outflow_cusecs")
    @classmethod
    def flow_range(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0 <= v <= MAX_PLAUSIBLE_FLOW_CUSECS):
            raise ValueError(f"flow {v} outside 0..{MAX_PLAUSIBLE_FLOW_CUSECS}")
        return v

    @field_validator("live_storage_maf")
    @classmethod
    def storage_range(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0 <= v <= 15):
            raise ValueError(f"live storage {v} MAF outside 0..15")
        return v

    @model_validator(mode="after")
    def level_range(self):
        s = STATIONS[self.station_id]
        if self.level_ft is not None and not (s.plausible_min_ft <= self.level_ft <= s.plausible_max_ft):
            raise ValueError(
                f"{s.name} level {self.level_ft} ft outside {s.plausible_min_ft}-{s.plausible_max_ft}")
        floor = MIN_PLAUSIBLE_INFLOW_CUSECS.get(self.station_id, 0)
        if self.inflow_cusecs is not None and self.inflow_cusecs < floor:
            raise ValueError(f"{s.name} inflow {self.inflow_cusecs} below plausible minimum {floor}")
        if self.level_ft is None and self.inflow_cusecs is None and self.outflow_cusecs is None:
            raise ValueError("row has no level, inflow or outflow")
        return self


def validate_rows(
    rows: list[dict],
    *,
    dead_level_lookup: Callable[[str, date], Optional[float]] | None = None,
    previous_level: Callable[[str, date], Optional[float]] | None = None,
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Return (good_rows_ready_for_upsert, [(raw_text, reason), ...])."""
    good: dict[tuple, dict] = {}
    bad: list[tuple[str, str]] = []

    for raw in rows:
        raw_text = json.dumps(raw, default=str, sort_keys=True)
        try:
            obs = Observation(**raw)
        except ValidationError as e:
            bad.append((raw_text, "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())))
            continue
        row = obs.model_dump()
        key = (obs.date, obs.station_id)

        if key in good:
            prev = good[key]
            same = all(prev.get(k) == row.get(k) for k in ("level_ft", "inflow_cusecs", "outflow_cusecs"))
            if not same:
                bad.append((raw_text, f"conflicting duplicate for {obs.station_id} {obs.date}"))
            continue

        # Day-to-day jump: compare with the previous day in this batch or the DB
        if obs.level_ft is not None:
            yday = obs.date - timedelta(days=1)
            prev_row = good.get((yday, obs.station_id))
            prev_lvl = prev_row["level_ft"] if prev_row else (
                previous_level(obs.station_id, yday) if previous_level else None)
            if prev_lvl is not None and abs(obs.level_ft - prev_lvl) > MAX_LEVEL_JUMP_FT:
                bad.append((raw_text, f"level jumped {obs.level_ft - prev_lvl:+.2f} ft in one day"))
                continue

        flags = []
        if dead_level_lookup and obs.level_ft is not None:
            dl = dead_level_lookup(obs.station_id, obs.date)
            if dl is not None and obs.level_ft < dl:
                flags.append("below_dead_level")
        row["flags"] = ",".join(flags) or None
        good[key] = row

    return list(good.values()), bad


def db_lookups(conn):
    """Build lookup callables backed by the database."""
    from sqlalchemy import select

    from . import db

    def dead(sid, d):
        return db.dead_level_on(conn, sid, d)

    def prev(sid, d):
        r = conn.execute(select(db.observations.c.level_ft).where(
            db.observations.c.station_id == sid, db.observations.c.date == d)).first()
        return r[0] if r else None

    return dead, prev
