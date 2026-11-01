# SentinelWatch Live Demonstration Guide

## Before the demo

Run these commands in the project folder. Use the interpreter that has the dependencies installed.

```powershell
python -m pip install -r requirements-core.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`, then verify `http://127.0.0.1:8000/api/health` and OpenAPI at `http://127.0.0.1:8000/docs`. The application initializes its SQLite schema at startup. Do not copy real values into `.env.example`; make a local `.env` with CDSE/SMTP settings only when authorized.

## Safe core demonstration

1. Show the dashboard title and state the boundary: it detects potential physical change, not illegality.
2. Show **Live data status**. Explain that `connected (sqlite)` means the local database is reachable; it does not prove satellite imagery has been downloaded.
3. Select **Add monitoring area** and explain AOI input/drawing. Create a small real/public area only if the reviewer permits storing it locally; otherwise show the form without submitting.
4. Show **Discover priority areas**. Explain it queries catalogue availability over grid cells and marks results as preliminary candidates, not incidents.
5. If CDSE metadata access is available, submit an AOI and choose **Check now**. The API initially returns a monitoring-run ID because processing is background work. Then refresh area/run data and inspect acquisition/provenance fields.
6. Show `/api/system/status` or dashboard system status. Explain every warning honestly—credentials, Rasterio, scheduler and model status.
7. If a real analysis completes, inspect its case/finding/alert, assign a department, inspect notification attempts, then use the case report route. Say exactly which step supplied each item.

## Preparing the optional operational path

1. Install the additional prerequisites only if you need them:

```powershell
python -m pip install -r requirements-operational.txt
```

2. Obtain authorized CDSE OAuth configuration and put it in local environment variables documented by `.env.example`.
3. Restart the service and confirm that system status no longer says imagery credentials/Rasterio are unavailable.
4. Choose a small AOI and dates with a reasonable cloud cover. A catalogue result is not automatically a usable pair; inspect date spacing and quality.
5. Use `POST /api/areas/{aoi_id}/check-now` via the dashboard or API docs. Preserve the run ID, provider product IDs and returned status for your evidence.

## Alerts, departments and reports

- Add a review department through the dashboard/API.
- Assign a genuine alert only after one exists. Inspect `GET /api/alerts/{alert_id}/notifications` to show whether the notification was merely recorded, not configured, sent or failed.
- Generate the case report only for a real stored case. If ReportLab is unavailable, say the report module is optional and unverified, not that the report was generated.

## If external services fail

- **No catalogue response:** show the error/refresh status; do not retry indefinitely or invent observations.
- **No suitable pair:** explain cloud/date constraints and show that the run ends without an analysis.
- **No CDSE OAuth/Rasterio:** demonstrate metadata/UI/API/SQLite modules and point to the transparent blocked status.
- **No trained model:** show `NOT_TRAINED`; describe baseline code as the current analysis method, not AI performance.
- **No SMTP:** show recorded notification status and say delivery was not demonstrated.

## Backup 2-minute sequence

Start the app, open the dashboard, show `/api/health`, status cards, model governance, and `/docs`. Explain the as-built architecture and run `python -m pytest -q` plus `python -m ruff check .` beforehand. This demonstrates verified platform behaviour without pretending an external imagery pipeline is live.

## Demo data

`python -m app.demo_seed` can create synthetic fixtures only if you explicitly choose to use it. Set `DEMO_MODE=true` only for that labelled demonstration, say “synthetic demo data” on every relevant slide, and never mix it with claims about live satellite results.
