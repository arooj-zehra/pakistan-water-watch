"""Pakistan Water Watch API.

    uvicorn api.main:app --reload          # local, http://127.0.0.1:8000/docs

Reads the same database as the pipeline (DATABASE_URL, or the local SQLite
file). Read-only: nothing here writes to the database. The /api/bundle/<name>
endpoints return exactly what pipeline.export_static writes to
web/public/data/, so the website can use either.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date
from functools import lru_cache
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from pipeline import db, queries
from pipeline.config import ROOT, STATIONS

Station = Literal["tarbela", "mangla"]

app = FastAPI(
    title="Pakistan Water Watch API",
    version="0.4.0",
    description=(
        "Daily level, inflow and outflow for Tarbela and Mangla reservoirs, reconstructed from "
        "WAPDA/IRSA daily releases as republished by APP, UrduPoint, Pakistan Observer and others, "
        "plus PMD Flood Forecasting Division sheets. Weather data by Open-Meteo.com (CC BY 4.0). "
        "Flows are 24-hour means to 06:00 PKT; levels in feet, flows in cusecs, storage in MAF."
    ),
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


@lru_cache(maxsize=1)
def engine():
    return db.get_engine()


def _check_dates(start: Optional[date], end: Optional[date]) -> None:
    if start and end and start > end:
        raise HTTPException(400, detail="'from' must be on or before 'to'")


@app.get("/health", tags=["service"])
def health():
    with engine().connect() as c:
        newest = c.execute(select(func.max(db.observations.c.date))).scalar()
    return {"status": "ok", "latest_date": newest.isoformat() if newest else None}


@app.get("/api/latest", tags=["data"], summary="Newest figures per dam")
def latest():
    with engine().connect() as c:
        return queries.latest(c)


@app.get("/api/history", tags=["data"], summary="Daily series for one dam")
def history(station: Station, start: Optional[date] = Query(None, alias="from"),
            end: Optional[date] = Query(None, alias="to")):
    _check_dates(start, end)
    with engine().connect() as c:
        rows = queries.history_rows(c, station, start, end)
    return {"station": station, "from": start, "to": end, "count": len(rows), "rows": rows}


@app.get("/api/climatology", tags=["data"], summary="Normal range per day of year")
def climatology(station: Optional[Station] = None):
    with engine().connect() as c:
        return queries.climatology(c, station)


@app.get("/api/sources", tags=["data"], summary="Where the numbers come from")
def sources():
    with engine().connect() as c:
        return {"sources": queries.sources(c)}


@app.get("/api/export.csv", tags=["data"], summary="Download daily data as CSV")
def export_csv(station: Optional[Station] = None, start: Optional[date] = Query(None, alias="from"),
               end: Optional[date] = Query(None, alias="to")):
    _check_dates(start, end)
    stations = [station] if station else list(STATIONS)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "station", *queries.FIELDS, "source"])
    with engine().connect() as c:
        for sid in stations:
            for r in queries.history_rows(c, sid, start, end):
                w.writerow([r["date"], sid, *(r[f] for f in queries.FIELDS), r["source"]])
    name = f"pakistan-water-watch-{station or 'all'}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


MODEL_FILES = ROOT / "web" / "public" / "data"  # written by models.daily (the daily job)


def _model_file(name: str) -> dict | None:
    path = MODEL_FILES / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


@app.get("/api/forecast", tags=["forecasts"], summary="Today's inflow forecasts with 50/80/90% ranges")
def forecast(station: Optional[Station] = None):
    doc = _model_file("forecast")
    if doc is None:
        raise HTTPException(404, "No forecasts yet: the daily job writes them once a model is trained.")
    if station:
        doc = {**doc, "stations": {station: doc["stations"].get(station)}}
    return doc


@app.get("/api/accuracy", tags=["forecasts"], summary="Live forecast scores against reported inflows")
def accuracy():
    doc = _model_file("accuracy")
    if doc is None:
        raise HTTPException(404, "No forecasts scored yet.")
    return {**doc, "monitor": _model_file("monitor")}


@app.get("/api/bundle/{name}", tags=["website"], summary="The website's data files, served live")
def bundle(name: Literal["latest", "history", "climatology", "meta", "forecast", "accuracy", "monitor"]):
    if name in ("forecast", "accuracy", "monitor"):
        doc = _model_file(name)
        if doc is None:
            raise HTTPException(404, f"{name}.json has not been written yet")
        return doc
    if name == "meta":
        return queries.meta(engine())
    with engine().connect() as c:
        return {"latest": queries.latest, "history": queries.history, "climatology": queries.climatology}[name](c)
