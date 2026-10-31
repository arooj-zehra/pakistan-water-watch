# Pakistan Water Watch

[![ci](https://github.com/arooj-zehra/pakistan-water-watch/actions/workflows/ci.yml/badge.svg)](https://github.com/arooj-zehra/pakistan-water-watch/actions/workflows/ci.yml)
[![daily_ingest](https://github.com/arooj-zehra/pakistan-water-watch/actions/workflows/daily_ingest.yml/badge.svg)](https://github.com/arooj-zehra/pakistan-water-watch/actions/workflows/daily_ingest.yml)

**A live inflow-forecasting system for Tarbela and Mangla reservoirs, with calibrated uncertainty, evaluated prospectively against real-world outcomes.**

> 🚧 Work in progress. Live link, dashboard GIF and results tables will go here.

## Status

| Phase | What | Status |
| --- | --- | --- |
| 0 | Data feasibility: PMD archive probe, ERA5 weather, coverage report | Code ready, run pending |
| 1 | Scraper, validation, database, daily GitHub Actions job | Code ready, needs DB + first runs |
| 2 | FastAPI + React dashboard | Not started |
| 3 | LightGBM forecasts with conformal 90% intervals | Not started |
| 4 | Live prospective scoring, model versioning, drift monitoring | Not started |
| 5 | Data-quality anomaly detection (+ optional bilingual ask-the-data) | Not started |
| 6 | Deployment and write-up | Not started |

## How it works

```
 PMD monthly sheets ─┐
 WAPDA daily release ─┼─► parse ─► validate ─┬─► PostgreSQL ◄── models (Phase 3-4)
 Open-Meteo ERA5/fcst ┘                      └─► quarantine        ▲
                                                                   │
                   GitHub Actions cron (05:00 UTC) ──── FastAPI ◄──┴──► React site
```

* Rows that fail validation go to `quarantine`, never to `observations`.
* Every job writes to `ingest_log`; a parser failure or stale data opens a GitHub issue.
* Forecasts are written once; a database trigger blocks UPDATE and DELETE so live scores stay honest.
* Raw HTML snapshots are committed under `data/raw/`, so anything can be re-parsed later.

## Run it locally

Needs Python 3.10+. From the repo folder (Windows PowerShell shown; on macOS/Linux use `source .venv/bin/activate`):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest                      # 28 tests, no network needed
```

**Phase 0: is there enough data?**

```powershell
python -m pipeline.probe_pmd          # ~7 min: downloads every monthly PMD sheet since 2010
python -m pipeline.fetch_weather      # ERA5 history for 10 upper-basin points
python -m pipeline.coverage_report    # GO / NO-GO verdict + docs/data_inventory.md + heatmap
```

Then open `notebooks/00_coverage.ipynb` for the detail.

**Phase 1: load the database and run the daily job**

```powershell
copy .env.example .env                # optional: add a Neon/Supabase DATABASE_URL (SQLite by default)
python -m pipeline.db init
python -m pipeline.backfill           # loads cached PMD sheets + ERA5 CSVs
python -m pipeline.daily              # what GitHub Actions runs every morning
python -m pipeline.daily --skip-pmd --skip-weather --press-file release.txt --date 2026-10-31
                                      # parse a saved WAPDA release by hand
```

## Repo layout

```
pipeline/   scrapers, parsers, validation, database, daily job
models/     features, training, conformal calibration      (Phase 3)
api/        FastAPI app                                    (Phase 2)
web/        React frontend                                 (Phase 2)
notebooks/  00_coverage, 03_backtest
tests/      parser fixtures, validation, database tests
docs/       data_inventory, MODEL_CARD, DATA_CARD
.github/    ci and daily_ingest workflows
```

## Data sources and attribution

* **WAPDA** daily river and reservoir releases (via republished press releases).
* **Pakistan Meteorological Department, Flood Forecasting Division**: monthly Tarbela & Mangla sheets (daily level, mean inflow and outflow at 06:00 PST).
* **Weather data by [Open-Meteo.com](https://open-meteo.com/)** (ERA5 reanalysis and forecasts), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

Scraping is limited to one request per page per day with an identifying User-Agent.

## Licence

MIT, see [LICENSE](LICENSE).
