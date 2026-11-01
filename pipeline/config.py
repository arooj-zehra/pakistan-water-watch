"""Static configuration: stations, weather points, paths, HTTP etiquette.

Anything here that is a physical fact about the dams carries a comment saying
where it came from or that it still needs verifying.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

try:  # optional: load .env when present
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_PMD_DIR = DATA_DIR / "raw" / "pmd"
RAW_PRESS_DIR = DATA_DIR / "raw" / "press"
WEATHER_DIR = DATA_DIR / "weather"
INVENTORY_DIR = DATA_DIR / "inventory"
DOCS_DIR = ROOT / "docs"

DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{(DATA_DIR / 'pww.db').as_posix()}"
PRESS_RELEASE_URL = os.environ.get("PRESS_RELEASE_URL") or None

# ---------------------------------------------------------------------------
# HTTP etiquette: identify ourselves, go slowly, never hammer a government site
# ---------------------------------------------------------------------------
USER_AGENT = (
    "PakistanWaterWatch/0.1 (student research project; "
    "+https://github.com/arooj-zehra/pakistan-water-watch)"
)
REQUEST_DELAY_S = 2.0  # pause between requests to the same host
REQUEST_TIMEOUT_S = 30


# ---------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Station:
    id: str
    name: str
    river: str
    max_level_ft: float  # current maximum conservation level
    # Hard physical bounds used by validation. Deliberately wide: they exist to
    # catch typos and swapped columns, not to judge operations. Historical
    # maximum/dead levels changed (Mangla was raised 2009-12, Tarbela's dead
    # level rose with sedimentation), so per-date limits live in dead_levels.
    plausible_min_ft: float
    plausible_max_ft: float


STATIONS: dict[str, Station] = {
    "tarbela": Station("tarbela", "Tarbela", "Indus", 1550.0, 1300.0, 1560.0),
    "mangla": Station("mangla", "Mangla", "Jhelum", 1242.0, 1000.0, 1250.0),
}

# Max plausible daily mean flow, cusecs. Tarbela's 2010 flood peak was in the
# 800k range; 1.5M leaves headroom while still catching an extra zero.
MAX_PLAUSIBLE_FLOW_CUSECS = 1_500_000
# Lowest plausible daily mean INFLOW. Winter lows are roughly 10-15k cusecs at
# Tarbela and a few thousand at Mangla, so anything below these is a parsing
# error (e.g. "224, 700" read as 224). Outflow has no floor: gates can be shut.
MIN_PLAUSIBLE_INFLOW_CUSECS = {"tarbela": 5_000, "mangla": 500}

# Dead level (minimum operating level) by date. VALUES/DATES NEED VERIFYING
# against archived WAPDA releases in Phase 0 task 5: the daily press release
# states "minimum operating level" so the daily parser records it, and you
# can backfill exact change dates from old releases. Until then these act as
# soft warnings, never as reasons to quarantine.
DEAD_LEVELS_SEED: list[tuple[str, date, float]] = [
    ("tarbela", date(2000, 1, 1), 1369.0),  # TODO verify valid_from
    ("tarbela", date(2015, 1, 1), 1380.0),  # TODO verify valid_from
    ("tarbela", date(2018, 1, 1), 1392.0),  # TODO verify valid_from
    ("tarbela", date(2021, 1, 1), 1398.0),  # TODO verify valid_from; APP confirms 1398 in Dec 2022 and Nov 2023
    ("tarbela", date(2024, 1, 1), 1402.0),  # TODO verify valid_from
    ("mangla", date(2000, 1, 1), 1040.0),  # TODO verify
    ("mangla", date(2022, 1, 1), 1050.0),  # TODO verify valid_from; APP confirms 1050 in Dec 2022 and Nov 2023
]


# ---------------------------------------------------------------------------
# Weather points (Open-Meteo ERA5). Upper Indus feeds Tarbela; upper Jhelum
# (incl. Neelum/Kunhar/Poonch) feeds Mangla. ERA5 is ~25 km so treat each
# point as a grid cell, not a station; high peaks will read warmer than reality.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WeatherPoint:
    id: str
    name: str
    catchment: str  # station id the point drives
    lat: float
    lon: float


WEATHER_POINTS: list[WeatherPoint] = [
    # Upper Indus -> Tarbela
    WeatherPoint("gilgit", "Gilgit", "tarbela", 35.92, 74.31),
    WeatherPoint("skardu", "Skardu", "tarbela", 35.30, 75.63),
    WeatherPoint("astore", "Astore", "tarbela", 35.37, 74.86),
    WeatherPoint("hunza", "Hunza (Karimabad)", "tarbela", 36.32, 74.66),
    WeatherPoint("leh", "Leh (Ladakh)", "tarbela", 34.16, 77.58),
    # Upper Jhelum -> Mangla
    WeatherPoint("srinagar", "Srinagar", "mangla", 34.08, 74.80),
    WeatherPoint("pahalgam", "Pahalgam", "mangla", 34.02, 75.32),
    WeatherPoint("muzaffarabad", "Muzaffarabad", "mangla", 34.37, 73.47),
    WeatherPoint("naran", "Naran (Kunhar)", "mangla", 34.91, 73.65),
    WeatherPoint("kel", "Kel (Neelum)", "mangla", 34.82, 74.35),
]

WEATHER_DAILY_VARS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
    "snowfall_sum",
]

# PMD archive: first month to probe
PMD_FIRST_MONTH = date(2010, 1, 1)
