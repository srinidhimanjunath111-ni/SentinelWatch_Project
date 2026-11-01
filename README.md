# SentinelWatch v3.0.0

SentinelWatch is a final-year engineering project for monitoring marked places in India using real Sentinel-2 imagery. An operator draws a site, chooses a catalogue-check interval, and reviews evidence of land change. The pipeline combines change segmentation, land-cover transitions, spectral indices, a same-season comparison, external context and a local permit registry. Eligible alerts are routed to the responsible department.

The strongest verdict is **“suspected unauthorised, no authorisation on record.”** Satellite imagery cannot establish illegality. Every finding needs human interpretation and, where appropriate, field verification. Seeded department addresses end in `.example`; they are illustrative destinations.

Sentinel-2 revisits a place roughly every **2–5 days**. Checking every 6, 12 or 24 hours, or weekly, polls the catalogue; it does not create a new image. Clouds can extend the wait. **Archive replay** processes real past imagery through the same pipeline and carries an explicit `archive_replay` badge.

## Screenshots

Capture genuine application screens before submission. These are placeholder links, not evidence that screenshots already exist:

- [Operations console](docs/screenshots/01-operations.png)
- [Site and scene timeline](docs/screenshots/02-site-timeline.png)
- [Incident evidence and comparison](docs/screenshots/03-incident.png)
- [Department inbox and dispatch outcomes](docs/screenshots/04-departments.png)
- [Models and data](docs/screenshots/05-models.png)
- [Permits and illustrative zones](docs/screenshots/06-zones.png)

See [capture instructions](docs/screenshots/README.md). No synthetic satellite result is a presentation fallback.

## Quick start

Use Python 3.12 and a virtual environment. Run from the repository root. The launch-script commands below are the intended entry points; their presence and successful launch remain **pending integrator check**. They were absent during the initial documentation inspection. Until supplied, use the equivalent manual setup below.

macOS/Linux:

```sh
bash scripts/run.sh
```

Windows PowerShell:

```powershell
.\scripts\run.ps1
```

Manual macOS/Linux setup:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-operational.txt scipy
cp .env.example .env
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Manual Windows PowerShell setup:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-operational.txt scipy
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Copy the configuration template only on first setup; preserve an existing `.env`. Open [the console](http://127.0.0.1:8000), [health](http://127.0.0.1:8000/api/health) or [API documentation](http://127.0.0.1:8000/docs). SQLite and storage directories are created at startup. Launch commands are instructions, not a claim that Windows or Linux execution was tested here.

Fresh-install gap at inspection: `app/services/analysis.py` imports `scipy.ndimage`, but the operational requirements do not explicitly declare SciPy. The manual commands above include it as a temporary dependency workaround; the backend requirements and launch scripts need integrator reconciliation. These installation commands were not run here. Operational dependencies are intended to support real raster processing and the labelled spectral fallback. Learned inference additionally needs `requirements-ml.txt` and compatible checkpoints. Training is still in progress at this documentation handoff; no measured model performance is asserted here. Read `/api/models` and `models/registry.json` for the installed model's actual status and evaluated facts. A loadable checkpoint and a held-out evaluation are different things.

## Configuration

Settings are read from environment variables and, when python-dotenv is available, `.env`. Never commit secret values. The public imagery providers do not require a personal account.

| Setting | Purpose / code default |
|---|---|
| `DATABASE_URL`, `STORAGE_DIR`, `LOG_DIR` | SQLite at `instance/sentinelwatch.db`; evidence/cache/outbox under `storage`; logs under `logs` |
| `EARTH_SEARCH_URL`, `PLANETARY_COMPUTER_URL` | Ordered public STAC providers; see `app/config.py` for endpoints |
| `COPERNICUS_CLIENT_ID`, `COPERNICUS_CLIENT_SECRET` | Optional CDSE fallback; never required for the public-provider path |
| `ENABLE_MONITORING_SCHEDULER`, `SCHEDULER_WORKERS` | Scheduled catalogue polling enabled by default; bounded worker pool |
| `DEFAULT_MONITORING_INTERVAL_MINUTES` | Default 1440; UI offers 6 h, 12 h, 24 h and weekly |
| `MONITORING_LOOKBACK_DAYS`, `MONITORING_MIN_PAIR_GAP_DAYS` | Default 180-day search; baseline at least 20 days before the later image |
| `MIN_AOI_CLEAR_FRACTION`, `MIN_PAIR_VALID_FRACTION` | Default 0.60 usable AOI coverage; 0.30 shared coverage for pair selection |
| `SEASONAL_REFERENCE_WINDOW_DAYS` | Search within 45 days either side of the date 365 days before the later scene |
| `MAX_CATCH_UP_DATES` | Default 12 recent dates retained for chronological live catch-up after an established baseline; older excess dates may be skipped and audited |
| `PRELOAD_MODELS` | Background model warm-up at startup, enabled by default |
| `MAX_AOI_REQUEST_PIXELS` | Default 1024 on the longest grid side, at 10 m; split larger sites |
| `CHANGE_CHECKPOINT`, `LANDCOVER_CHECKPOINT` | Optional checkpoint overrides; expected installed paths are under `models/checkpoints/` |
| `MODEL_REGISTRY_PATH`, `SENTINELWATCH_ML_DEVICE` | Registry path; optional inference device override (`cpu`, `mps`, `cuda`) |
| `SMTP_HOST`, `SMTP_FROM`, `SMTP_PORT`, `SMTP_STARTTLS` | Real SMTP transport only when configured |
| `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_REDIRECT_TO` | Optional login and a controlled recipient override for a permitted email demonstration |
| `SENTINELWATCH_ADMIN_PASSWORD` | Optional login for mutating API requests; reads remain public |
| `CORS_ORIGINS` | Explicit allowed browser origins |
| `SENTINELWATCH_SEED_SITES`, `SENTINELWATCH_RESTORE_SNAPSHOT` | First-run seeding / restoration if applicable; existing evidence is not a new live observation |

The local permit registry is not connected to a comprehensive official clearance database. Records with `is_illustrative=true` have approximate boundaries and must be described as illustrative. OpenStreetMap quarry/protected-area features provide context, never permission.

## Architecture and workflow

The browser uses locally vendored Leaflet and JavaScript modules. FastAPI serves the dashboard and JSON API. SQLAlchemy stores sites, scenes, runs, analyses, cases, findings, alerts, departments, notification attempts and audit events in SQLite. A single application process owns the scheduler and Server-Sent Events bus.

1. Create a site or open a seeded site; start a check or wait for its schedule.
2. Query Earth Search, then Planetary Computer on provider failure; use CDSE only when configured. Read cropped COG windows and cache them.
3. Select a usable later scene, an older baseline and, if available, a same-season reference from the previous year. Align six bands on one 10 m UTM grid; mask SCL cloud/shadow/no-data plus residual haze and buffer invalid edges.
4. Run a Siamese U-Net when loadable, combined with spectral evidence; otherwise label the spectral fallback. Classify land cover with a single-date U-Net or an explicitly recorded spectral fallback.
5. Classify connected regions, test seasonal persistence and date/type/overlap against registered zones. Store plain-language reasons and georeferenced evidence.
6. Retain seasonal findings without alerts. Apply evidence/category filters and duplicate merging to other findings. Route eligible alerts to a department inbox immediately; use SMTP or save an unsent `.eml` with `OUTBOX_SAVED`.
7. Review the before/after comparison, timeline, reasons, context and dispatch log. Record acknowledgement, field verification and an outcome. Download a case investigation PDF when reporting dependencies are available.

See [architecture and database diagrams](docs/diagrams/) and the [project report](docs/SentinelWatch_Project_Report.md). An alert marked `dispatched` records workflow routing; inspect its channel outcome before claiming an email was sent. SMTP `SENT` means server acceptance, not human receipt.

## Data sources

| Source | Use | Boundary |
|---|---|---|
| [Element 84 Earth Search](https://github.com/Element84/earth-search) | Primary Sentinel-2 L2A catalogue and COG assets | Public network service; availability is not guaranteed |
| [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a) | Public fallback catalogue and anonymously signed asset URLs | Network/signing can fail |
| [Copernicus Data Space](https://dataspace.copernicus.eu/data-collections/copernicus-sentinel-missions/sentinel-2) | Optional additional provider | Configured OAuth needed for its processing path |
| [OSCD / Daudt et al.](https://rcdaudt.github.io/oscd/) | Change-model paired imagery and urban labels | L1C domain; change-map licence is academic/noncommercial, see dataset terms |
| [Hansen Global Forest Change](https://glad.earthengine.app/view/global-forest-change) | Annual tree-loss supervision for India L2A pairs | Coarse annual labels, not exact event dates or legal findings |
| [ESA WorldCover 2021 v200](https://zenodo.org/records/7254221) | Land-cover reference labels | Model-derived labels; agreement is not field accuracy |
| [Maus et al. mining polygons v2](https://doi.pangaea.de/10.1594/PANGAEA.942325) | Mining-footprint supervision | Historical footprints, not permits or current activity |
| Open-Meteo; NASA EONET; GDACS | Rainfall and nearby disaster context | Optional; missing feeds do not prove absence of an event |
| OpenStreetMap / Overpass; Copernicus DEM | Mapped features and slope context | Incomplete context; no legal-authorisation inference |
| Local authorised-zone register | Date/type/overlap check | Coverage and provenance must be checked by a person |

Verified bibliography and source limitations are in the report. The helpers `acquire_sentinel.py` and `prepare_dataset.py` retain older metadata/RGB preparation uses; they are not the six-band training recipe below.

## Reproduce training and evaluation

These are resource-intensive reproduction instructions, **not commands run during documentation work**. Do not start them alongside an existing training job on the small presentation machine. Obtain the source datasets lawfully and retain their manifests/licences. The India builders expect `data/raw/naturalearth/ne_50m_admin_1.geojson`; land-cover building also expects `data/raw/mining_maus2022/global_mining_polygons_v2.gpkg`. Supply those inputs before running the builders.

```sh
.venv/bin/python -m pip install -r requirements-ml.txt
.venv/bin/python -m data_tools.fetch_oscd
.venv/bin/python -m data_tools.build_india_forest_change
.venv/bin/python -m data_tools.build_india_landcover
.venv/bin/python -m training.train --datasets oscd --version v1 --run-dir data/processed/runs/change_v1
.venv/bin/python -m training.evaluate export-change --run-dir data/processed/runs/change_v1 --report reports/ml/change_v1_training.json
.venv/bin/python -m training.evaluate change --checkpoint data/processed/runs/change_v1/best.pt --dataset oscd --report reports/ml/change_v1_oscd_test.json
.venv/bin/python -m training.train_landcover --version v1 --run-dir data/processed/runs/landcover_v1 --report reports/ml/landcover_v1_training.json
.venv/bin/python -m training.evaluate landcover --checkpoint data/processed/runs/landcover_v1/best.pt --report reports/ml/landcover_v1_test.json
```

The change CLI also supports `--norm batch|group`, `--normalisation pooled|per_date` and `--ema-decay`; defaults are not a statement of the final selected model. Inference follows checkpoint-recorded preprocessing. Per-date normalisation can cancel broad atmospheric differences but can also erase widespread change. Preserve the actual run configuration.

For a predeclared India-forest adaptation experiment, `training.train` accepts `--datasets oscd,india_forest`, `--india-share` and `--init-from`; evaluate its frozen checkpoint separately with `--dataset india_forest`. Keep validation for threshold/model selection and test for final reporting. Do not select a winner by repeatedly looking at test scores. OSCD uses city splits with validation cities removed from official training; the India datasets use geographic splits with margins.

After selection, install the chosen change checkpoint as `models/checkpoints/change_siamese.pt` and the chosen land-cover checkpoint as `models/checkpoints/landcover_unet.pt`. Preserve their evaluation reports, then run:

```sh
.venv/bin/python -m training.build_registry
```

Use the equivalent `.venv\Scripts\python.exe` on Windows. The registry builder matches held-out reports to checkpoint SHA-256; a checkpoint without matching evaluation is marked `UNEVALUATED`. Restart after replacing cached model instances. No accuracy, F1, IoU, sample count or runtime figure is claimed until the integrator reconciles the registry and report tokens.

## Deployment and verification

The repository supplies a Dockerfile and Compose file. **Docker is not installed on this documentation machine; deployment is unverified here.** The inspected Dockerfile installs operational dependencies, not the ML extra, and those operational requirements omit the SciPy import needed by analysis. Confirm the final image includes the intended inference dependencies and checkpoints before presenting container model inference.

```sh
docker compose up --build
```

Persist `instance/` and `storage/`; preserve checkpoints, `models/registry.json` and `reports/ml/` with the release. Use one Uvicorn process for the in-process scheduler. Multiple server processes would each own a scheduler; a distributed queue/lock is future work. For a public deployment, configure the optional write login and TLS/reverse proxy, and review the deliberately public read/evidence endpoints. This is not department authentication or role-based access control.

Relevant checks for the integrator are `python -m pytest -q` and `python -m ruff check .`. This documentation task does not claim a current full-suite pass. See the [completion checklist](docs/SentinelWatch_Completion_Checklist.md) for checks actually run and pending operational evidence.

## Limits and presentation material

Clouds, mixed pixels, registration error, seasonal water/vegetation, crop timing and weak reference labels can produce missed or false detections. Upsampling SWIR bands does not add 10 m detail. A land-cover label or a burn pattern does not prove cause or intent. Inference confidence and priority are evidence-ranking heuristics, not a probability of wrongdoing. A first check starts at the newest usable scene; subsequent live checks drain newer dates chronologically within a bounded catch-up budget. Excess older dates can be skipped and audited; use replay to inspect selected past dates. The local registry is incomplete and illustrative zones remain illustrative.

Read the [10-minute demo](docs/SentinelWatch_Demo_Guide.md), [speaking guide](docs/SentinelWatch_Review_Speaking_Guide.md), [viva questions](docs/SentinelWatch_Viva_Questions.md), [v3 release notes](RELEASE_NOTES.md) and [documentation index](docs/README.md). The old review PDF is obsolete; the integrator must remove it and build a new PDF from the final reconciled report. No new PDF was built in this task.
