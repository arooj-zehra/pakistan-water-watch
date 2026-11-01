# API

FastAPI app serving the same database the pipeline writes. Read-only.

```cmd
uvicorn api.main:app --reload
```

Then open http://127.0.0.1:8000/docs for the interactive documentation.

| Endpoint | What it returns |
| --- | --- |
| `GET /health` | `ok` and the newest date in the database |
| `GET /api/latest` | Newest figures per dam, the day before, dead and maximum level |
| `GET /api/history?station=tarbela&from=2026-01-01&to=2026-10-31` | Daily rows for one dam |
| `GET /api/climatology?station=mangla` | 10th, 50th and 90th percentile per day of year |
| `GET /api/sources` | Dam-days, first and last date per source |
| `GET /api/export.csv?station=&from=&to=` | The same rows as a CSV download |
| `GET /api/forecast`, `GET /api/accuracy` | Placeholders until Phases 3 and 4 |
| `GET /api/bundle/{latest,history,climatology,meta}` | Exactly the website's static data files |

Deployed on Render from `render.yaml` at the repo root; set `DATABASE_URL` there.
