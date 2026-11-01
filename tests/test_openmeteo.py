from datetime import date
from types import SimpleNamespace

import pytest

from pipeline import config, http
from pipeline.sources import openmeteo


def _resp(status, payload):
    return SimpleNamespace(status_code=status, json=lambda: payload, text=str(payload))


def _ok_payload():
    return {"daily": {"time": ["2024-01-01", "2024-01-02"],
                      **{v: [1.0, None] for v in config.WEATHER_DAILY_VARS}}}


def test_retries_after_rate_limit(monkeypatch):
    replies = iter([_resp(429, {"reason": "Minutely API request limit exceeded"}), _resp(200, _ok_payload())])
    monkeypatch.setattr(http, "get", lambda *a, **k: next(replies))
    waits = []
    rows = openmeteo.fetch_archive(config.WEATHER_POINTS[0], date(2024, 1, 1), date(2024, 1, 2), sleep=waits.append)
    assert waits == [openmeteo.RETRY_WAITS_S[0]]
    assert len(rows) == 2 and rows[0]["tmax_c"] == 1.0 and rows[1]["tmax_c"] is None


def test_other_errors_raise_with_reason(monkeypatch):
    monkeypatch.setattr(http, "get", lambda *a, **k: _resp(400, {"reason": "Latitude must be in range"}))
    with pytest.raises(openmeteo.OpenMeteoError, match="400: Latitude"):
        openmeteo.fetch_archive(config.WEATHER_POINTS[0], date(2024, 1, 1), date(2024, 1, 2), sleep=lambda s: None)


def test_gives_up_after_repeated_rate_limits(monkeypatch):
    monkeypatch.setattr(http, "get", lambda *a, **k: _resp(429, {"reason": "Hourly API request limit exceeded"}))
    with pytest.raises(openmeteo.OpenMeteoError, match="429"):
        openmeteo.fetch_archive(config.WEATHER_POINTS[0], date(2024, 1, 1), date(2024, 1, 2), sleep=lambda s: None)


def test_skip_complete_csv(tmp_path):
    from pipeline.fetch_weather import already_complete

    p = tmp_path / "era5_x.csv"
    assert not already_complete(p, date(2010, 1, 1), date(2026, 10, 26))
    p.write_text("date,point_id,model\n2010-01-01,x,era5\n2026-10-25,x,era5\n")
    assert already_complete(p, date(2010, 1, 1), date(2026, 10, 26))
    assert not already_complete(p, date(2009, 1, 1), date(2026, 10, 26))
    # files from before the model was pinned (no model column) are refetched
    p.write_text("date,point_id\n2010-01-01,x\n2026-10-25,x\n")
    assert not already_complete(p, date(2010, 1, 1), date(2026, 10, 26))


def test_archive_requests_pin_era5(monkeypatch):
    seen = {}

    def fake_get(url, **kw):
        seen.update(kw["params"])
        return _resp(200, _ok_payload())

    monkeypatch.setattr(http, "get", fake_get)
    rows = openmeteo.fetch_archive(config.WEATHER_POINTS[0], date(2024, 1, 1), date(2024, 1, 2))
    assert seen["models"] == "era5" and rows[0]["model"] == "era5"
