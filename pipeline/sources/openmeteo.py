"""Open-Meteo: ERA5 reanalysis (history, ~5 day lag) and 16-day forecasts.

Free for non-commercial use, no API key, CC BY 4.0: credit Open-Meteo on the
site. Docs: https://open-meteo.com/en/docs/historical-weather-api
"""
from __future__ import annotations

from datetime import date

from ..config import WEATHER_DAILY_VARS, WeatherPoint

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TZ = "Asia/Karachi"

# Our column names <- Open-Meteo variable names
COLUMN_MAP = {
    "temperature_2m_max": "tmax_c",
    "temperature_2m_min": "tmin_c",
    "temperature_2m_mean": "tmean_c",
    "precipitation_sum": "precip_mm",
    "snowfall_sum": "snowfall_cm",
}


def _rows_from_payload(payload: dict, point: WeatherPoint, kind: str) -> list[dict]:
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    rows = []
    for i, d in enumerate(dates):
        row = {"date": date.fromisoformat(d), "point_id": point.id, "kind": kind}
        for src, dst in COLUMN_MAP.items():
            vals = daily.get(src)
            row[dst] = vals[i] if vals is not None else None
        rows.append(row)
    return rows


def fetch_archive(point: WeatherPoint, start: date, end: date) -> list[dict]:
    from .. import http

    r = http.get(ARCHIVE_URL, delay=1.0, params={
        "latitude": point.lat, "longitude": point.lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": ",".join(WEATHER_DAILY_VARS), "timezone": TZ,
    }, timeout=120)
    r.raise_for_status()
    return _rows_from_payload(r.json(), point, "era5")


def fetch_forecast(point: WeatherPoint, days: int = 16) -> list[dict]:
    from .. import http

    r = http.get(FORECAST_URL, delay=1.0, params={
        "latitude": point.lat, "longitude": point.lon,
        "daily": ",".join(WEATHER_DAILY_VARS), "timezone": TZ,
        "forecast_days": days,
    })
    r.raise_for_status()
    return _rows_from_payload(r.json(), point, "forecast")
