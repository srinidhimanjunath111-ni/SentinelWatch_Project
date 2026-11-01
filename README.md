# SentinelWatch

Persistent FastAPI/SQLite satellite monitoring application for potential vegetation loss, land disturbance and construction-like change. It uses the official Copernicus Data Space Ecosystem STAC catalogue for real Sentinel-2 L2A metadata. Pixel retrieval uses CDSE's authenticated Process API; no satellite image or alert is fabricated when credentials are unavailable.

## Run locally

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-operational.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open: http://127.0.0.1:8000

The local database is `instance/sentinelwatch.db`; cached evidence/raster assets are under `storage/`. Both are created automatically. Copy `.env.example` to `.env` to configure CDSE OAuth credentials and optional SMTP notifications.

## Main workflow

1. Draw a monitoring area, or use **Discover priority areas** with your own region.
2. The application calls CDSE STAC, records actual product metadata, and chooses a comparable pair.
3. With credentials, it retrieves and preprocesses bands and runs the transparent baseline or a validated installed Siamese model.
4. Material findings create a reviewable alert, case, audit record and PDF report.

Grid candidates are explicitly labelled preliminary. They rank catalogue freshness, cloud quality and availability; they are not claims that an incident was detected. Satellite results require human verification and do not prove illegality.

## Clearly labelled offline demonstration mode

When CDSE is unavailable for a classroom demonstration, you can create local synthetic fixtures explicitly and visibly labelled **DEMO DATA**:

```powershell
python -m app.demo_seed
$env:DEMO_MODE="true"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Demo fixtures never appear in ordinary mode and must not be represented as live satellite analysis or model validation.

## Optional model training

```powershell
python -m pip install -r requirements-ml.txt
python -m training.train --data data/processed/your_dataset --epochs 40 --batch-size 8
python -m training.evaluate --data data/processed/your_dataset --checkpoint models/checkpoints/siamese_unet_best.pt
```

## Quality checks

```powershell
python -m pytest -q
python -m ruff check .
```

## External configuration

- `COPERNICUS_CLIENT_ID` and `COPERNICUS_CLIENT_SECRET`: required only for CDSE pixel retrieval; metadata search is public.
- `SMTP_HOST` and `SMTP_FROM`: required before an email notification can be sent. Otherwise an honest `NOT_CONFIGURED` notification record is retained.
- No training dataset or trained checkpoint is bundled. The dashboard shows `NOT_TRAINED` until a lawful dataset produces a real held-out evaluation report.
# Review documentation

The final-year review package is in [docs/README.md](docs/README.md). It records the verified local status and deliberately distinguishes implemented code from live integrations that still need configuration and evidence.

