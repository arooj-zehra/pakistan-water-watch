"""Open-Meteo: ERA5 reanalysis (history, ~5 day lag) and 16-day forecasts.

Free for non-commercial use, no API key, CC BY 4.0: credit Open-Meteo on the
site. Docs: https://open-meteo.com/en/docs/historical-weather-api
"""
from __future__ import annotations

from datetime import date

from ..config import WEATHER_DAILY_VARS, WeatherPoint

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
# Pin the reanalysis model. The default ("best match") blends in the 9 km
# ECMWF IFS from 2017 onward, which shows up as a step change of several °C in
# upper-Indus winter temperatures around 2017 and would be learned by the model
# as if it were climate. Open-Meteo advises ERA5 or ERA5-Land alone for
# multi-decade work; ERA5-Land has no snowfall, so we use ERA5.
ARCHIVE_MODEL = "era5"
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


class OpenMeteoError(RuntimeError):
    pass


# Open-Meteo's free tier limits weighted calls per minute/hour/day, and a
# multi-year request counts as hundreds of calls. When it answers 429 (too many
# requests), wait for the minute window to reset and try again.
RETRY_WAITS_S = (65, 65, 120)


def _get_with_retry(url: str, params: dict, timeout: int, sleep=None) -> dict:
    import time

    from .. import http

    sleep = sleep or time.sleep
    for attempt in range(len(RETRY_WAITS_S) + 1):
        r = http.get(url, delay=1.0, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        try:
            reason = r.json().get("reason", r.text[:200])
        except Exception:
            reason = getattr(r, "text", "")[:200]
        if r.status_code == 429 and attempt < len(RETRY_WAITS_S):
            wait = RETRY_WAITS_S[attempt]
            print(f"    rate limited ({reason}); waiting {wait}s then retrying...", flush=True)
            sleep(wait)
            continue
        raise OpenMeteoError(f"HTTP {r.status_code}: {reason}")
    raise OpenMeteoError("gave up after retries")  # pragma: no cover


def fetch_archive(point: WeatherPoint, start: date, end: date, sleep=None) -> list[dict]:
    payload = _get_with_retry(ARCHIVE_URL, {
        "latitude": point.lat, "longitude": point.lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": ",".join(WEATHER_DAILY_VARS), "timezone": TZ,
        "models": ARCHIVE_MODEL,
    }, timeout=120, sleep=sleep)
    rows = _rows_from_payload(payload, point, "era5")
    for r in rows:
        r["model"] = ARCHIVE_MODEL
    return rows


def fetch_forecast(point: WeatherPoint, days: int = 16, sleep=None) -> list[dict]:
    payload = _get_with_retry(FORECAST_URL, {
        "latitude": point.lat, "longitude": point.lon,
        "daily": ",".join(WEATHER_DAILY_VARS), "timezone": TZ,
        "forecast_days": days,
    }, timeout=30, sleep=sleep)
    return _rows_from_payload(payload, point, "forecast")
