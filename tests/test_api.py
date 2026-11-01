from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from pipeline import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    from api import main

    eng = db.get_engine(f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    db.init_db(eng)
    start = date(2026, 10, 1)
    rows = []
    for i in range(30):
        d = start + timedelta(days=i)
        rows.append({"date": d, "station_id": "tarbela", "level_ft": 1500 + i * 0.5, "inflow_cusecs": 30000.0 + i,
                     "outflow_cusecs": 50000.0, "live_storage_maf": None, "source": "app", "fetched_at": db.utcnow()})
        rows.append({"date": d, "station_id": "mangla", "level_ft": 1190.0, "inflow_cusecs": 7000.0,
                     "outflow_cusecs": 16000.0, "live_storage_maf": 4.1, "source": "urdupoint", "fetched_at": db.utcnow()})
    with eng.begin() as c:
        db.upsert(c, db.observations, rows, ["date", "station_id"])
    main.engine.cache_clear()
    monkeypatch.setattr(main, "engine", lambda: eng)
    return TestClient(main.app)


def test_health_and_latest(client):
    assert client.get("/health").json() == {"status": "ok", "latest_date": "2026-10-30"}
    st = {s["id"]: s for s in client.get("/api/latest").json()["stations"]}
    t = st["tarbela"]
    assert t["date"] == "2026-10-30" and t["level_ft"] == 1514.5 and t["previous"]["level_ft"] == 1514.0
    assert t["dead_level_ft"] == 1402.0 and t["max_level_ft"] == 1550.0 and t["source_name"].startswith("Associated")
    assert st["mangla"]["live_storage_maf"] == 4.1


def test_history_filters_and_validation(client):
    r = client.get("/api/history", params={"station": "mangla", "from": "2026-10-10", "to": "2026-10-12"}).json()
    assert r["count"] == 3 and r["rows"][0]["date"] == "2026-10-10"
    assert client.get("/api/history", params={"station": "warsak"}).status_code == 422
    assert client.get("/api/history", params={"station": "tarbela", "from": "2026-10-12", "to": "2026-10-01"}).status_code == 400


def test_export_csv(client):
    r = client.get("/api/export.csv", params={"station": "tarbela", "from": "2026-10-29"})
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0] == "date,station,level_ft,inflow_cusecs,outflow_cusecs,live_storage_maf,source"
    assert len(lines) == 3 and lines[1].startswith("2026-10-29,tarbela,1514.0,30028.0")


def test_bundles_match_static_export_shapes(client):
    for name in ("latest", "history", "climatology", "meta"):
        assert client.get(f"/api/bundle/{name}").status_code == 200
    h = client.get("/api/bundle/history").json()["stations"]["tarbela"]
    assert len(h["date"]) == 30 and len(h["inflow_cusecs"]) == 30
    c = client.get("/api/climatology", params={"station": "tarbela"}).json()["stations"]["tarbela"]
    assert len(c["inflow_cusecs"]["p50"]) == 366
    m = client.get("/api/bundle/meta").json()
    assert m["latest_date"] == "2026-10-30" and m["sources"][0]["dam_days"] == 30


def test_forecast_endpoints_and_docs(client, tmp_path, monkeypatch):
    import json

    from api import main

    monkeypatch.setattr(main, "MODEL_FILES", tmp_path)
    assert client.get("/api/forecast").status_code == 404
    (tmp_path / "forecast.json").write_text(json.dumps({"model_version": "v1", "stations": {"tarbela": {"x": 1}, "mangla": {"x": 2}}}))
    (tmp_path / "accuracy.json").write_text(json.dumps({"scored": 0}))
    assert client.get("/api/forecast?station=mangla").json()["stations"] == {"mangla": {"x": 2}}
    assert client.get("/api/accuracy").json() == {"scored": 0, "monitor": None}
    assert client.get("/api/bundle/forecast").json()["model_version"] == "v1"
    assert client.get("/api/bundle/monitor").status_code == 404
    assert client.get("/docs").status_code == 200
    assert "/api/latest" in client.get("/openapi.json").json()["paths"]
