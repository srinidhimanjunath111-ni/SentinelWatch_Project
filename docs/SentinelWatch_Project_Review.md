# SentinelWatch Project Review

> Evidence baseline: source inspected and local application verified on 12 October 2026. The dashboard was running at `http://127.0.0.1:8001`; health returned `200 OK`; `pytest -q` reported **19 passed** and `ruff check .` passed. This is not proof of a complete operational satellite-analysis workflow.

## 1. Project Overview

**Title:** SentinelWatch — AI-assisted satellite land-change monitoring. **Domain:** remote sensing, GIS, machine learning, and web information systems.

**Problem.** Large areas are difficult to inspect repeatedly on the ground. Vegetation loss, excavation, construction and other physical land-surface changes can go unnoticed for weeks. SentinelWatch helps an operator define an Area of Interest (AOI), find Sentinel-2 observations for it, compare two dates when the required services are configured, and place potential changes in a review queue.

**Important boundary:** it detects *possible physical change*, not crime. An alert is a lead requiring imagery-quality, seasonality, permits, and local verification. It cannot legally prove illegal cutting, mining, or construction.

**Motivation and proposed solution.** Instead of manually browsing image catalogues, the system keeps AOIs, searches the official Copernicus catalogue, records provenance, preprocesses suitable image pairs, runs a baseline or optional model, calculates change regions, and creates reviewable cases/alerts. Its intended users are forest, planning, environment, or research reviewers. Benefits are a repeatable evidence trail and a way to prioritize human attention.

**Objectives and scope.** The implemented scope is a FastAPI dashboard, SQLite persistence, CDSE catalogue adapter, monitoring-run orchestration, an optional GeoTIFF path, baseline change detection, optional Siamese U-Net code, alert/case workflow, department routing, notification attempt records, PDF case-report code, training/evaluation scripts, and tests. It is not yet a validated production surveillance service: no trained checkpoint, credentials, Rasterio processing runtime, SMTP delivery, or real end-to-end analysis result was verified in this environment.

**Key assumptions/limits.** An AOI must be supplied; a suitable cloud-limited observation pair must exist; authenticated imagery access and Rasterio are needed for operational raster processing; any model must have a lawful labelled dataset and validated checkpoint. Clouds, shadows, seasonal growth, imperfect alignment and resolution can resemble change.

**30-second explanation.** “SentinelWatch is a decision-support web application. A user marks an area, the system searches official Sentinel-2 satellite metadata, and—when imagery services are configured—compares two dates for potential land change. It records the evidence and raises a review alert. It never claims that a change is automatically illegal.”

**One-minute explanation.** “The project combines a browser dashboard, a Python FastAPI backend, SQLite database and Copernicus Data Space catalogue. Areas and monitoring runs are stored locally. The backend finds observation metadata, chooses a before/after pair, then can retrieve six-band imagery, mask poor pixels, calculate spectral/NDVI differences and extract changed regions. A Siamese U-Net is available in code but there is no trained checkpoint installed, so the honest present state is a labelled baseline path plus model-training scaffolding. Findings can become cases, alerts, department assignments and a PDF report.”

**Two-minute explanation.** “The practical problem is that field inspection cannot cover every large or remote area. SentinelWatch therefore starts with a specific AOI and dates rather than making a vague accusation. It uses the CDSE STAC catalogue to discover official Sentinel-2 products. Metadata such as product ID, acquisition time and cloud cover is stored so a reviewer can trace where evidence came from. If suitable images and configuration are available, the backend requests imagery, aligns the rasters, masks cloud/shadow pixels, compares vegetation and visible-band signals, cleans the change mask, and turns connected changed pixels into regions with physical area. It can label a region as vegetation loss, construction-like change or land disturbance; that label is a screening classification, not a legal conclusion. Findings, cases, alerts and reviewer notes are persisted. The dashboard makes limitations visible: this local run has no image credentials, Rasterio or validated model, so it shows those blockers instead of producing invented results. Unit tests verify many code contracts, but a live data-to-alert demonstration still requires configuration and real imagery.”

## 2. System Architecture

![As-built architecture source](diagrams/architecture.mmd)

The **frontend** is a static Jinja-served HTML/CSS/JavaScript dashboard using Leaflet when its CDN assets load. It calls JSON endpoints in the **FastAPI API layer**. Routers in `app/api/monitoring.py` and `app/api/operations.py` validate requests and write through SQLAlchemy into the local **SQLite** database (`instance/sentinelwatch.db`). `app/services/monitoring.py` executes a queued run in a FastAPI background task (and contains optional scheduler logic); it searches CDSE metadata, stores refresh/acquisition records and only moves to imagery processing if prerequisites exist.

`app/satellite/copernicus.py` is the external adapter: STAC metadata search is public; Process API raster retrieval requires OAuth credentials. `app/services/geospatial.py` optionally needs Rasterio/PyProj to align, mask and calculate spatial output. `app/services/analysis.py` uses the numpy/Pillow baseline or `app/ml/inference.py` only when a loadable model checkpoint exists. Results become database analysis, case, finding and alert records. `app/services/alerting.py`, `notifications.py`, and `reporting.py` respectively create review alerts, record/attempt optional SMTP delivery, and generate a case PDF when ReportLab is available.

Errors are represented as failed/blocked monitoring states and provider-refresh records rather than fabricated imagery. Actual architecture differs from the target: the target includes credentials, Rasterio, a validated checkpoint, reliable background worker/scheduler and SMTP; the inspected local run lacks all of those operational prerequisites.

## 3. Module Description

| Module | Purpose | Inputs | Processing | Outputs | Main files/classes/APIs | Current status |
|---|---|---|---|---|---|---|
| Dashboard | Operate and view status | Browser actions | Fetch/render JSON | AOI/case/alert views | `templates/index.html`, `static/app.js` | Implemented and manually opened |
| AOI management | Persist monitoring areas | geometry/name | validation, pause/resume | area record | `api/monitoring.py` `/areas` | Implemented and tested |
| Priority discovery | Suggest grid cells with catalogue availability | region/bbox | STAC per cell and heuristic score | preliminary candidates | `priority_discovery.py`, `/priority-candidates` | Implemented and tested with mock provider; not change detection |
| Catalogue search | Find official product metadata | AOI/time/cloud filters | CDSE STAC POST search | acquisitions | `satellite/copernicus.py` | Implemented; live metadata query verified |
| Monitoring orchestration | Run the workflow | area/run ID | metadata, pair choice, state/audit | monitoring status | `services/monitoring.py` | Implemented and unit-tested; full path unverified |
| Geospatial preparation | Make rasters comparable | downloaded bands | align, reproject, masks, NDVI | arrays/metadata | `services/geospatial.py` | Implemented but not verified locally (Rasterio absent) |
| Change analysis | Locate candidate change | before/after arrays | baseline or optional model, components | findings | `services/analysis.py` | Baseline unit-tested; live imagery path unverified |
| Siamese model | Learned binary change map | paired tensors | shared encoder U-Net inference | probability mask | `ml/siamese_unet.py`, `inference.py` | Partially implemented; no checkpoint/evaluation |
| Alerts/routing | Prioritize human review | findings/department | threshold, dedup, notification attempt | alert/status | `alerting.py`, `notifications.py` | Implemented and tested; SMTP not configured |
| Persistence | Evidence trail | all records | SQLAlchemy relations | SQLite records | `models.py`, `migrations.py` | Implemented and tested |
| Reporting | Produce case PDF | stored case | ReportLab document | PDF response | `reporting.py` | Implemented but live PDF generation not verified |
| Training/evaluation | Train/measure a model | labelled dataset | BCE/Dice training, metrics | checkpoint/metrics | `training/` | Scaffolding only; no dataset/results |

**Beginner view of the main modules.** The dashboard is the control room. AOI management tells the system where to look. The catalogue adapter is a library search: it finds product descriptions, not automatically downloaded pixels. The monitoring service coordinates steps. Geospatial preparation makes two images line up and removes unreliable pixels. Analysis highlights suspicious pixels; alerting puts meaningful findings in a human queue. The database remembers every item, while reports make a case easy to share.

## 4. Data Flow Diagrams

Sources: [Level 0](diagrams/dfd-level-0.mmd), [Level 1](diagrams/dfd-level-1.mmd), [Level 2](diagrams/dfd-level-2.mmd).

**Level 0.** The operator sends AOIs and review actions to SentinelWatch. SentinelWatch queries CDSE and returns dashboard views/reports. Optional email is an outbound side effect only after configuration. Arrows mean data requests/responses, not proof that every external service is active.

**Level 1.** Process 1 stores areas/candidates in the SQLite data store. Processes 2–4 create a run, search metadata and choose dates. Processes 5–6 occur only with imagery prerequisites. Process 7 stores review work; process 8 presents records. The database is the only shown internal data store.

**Level 2.** The key workflow persists catalogue metadata, selects a temporal pair, checks prerequisites, requests imagery, preprocesses it, then uses either validated-model inference or the baseline. Thresholding and connected components turn a pixel mask into a physical finding. If prerequisites are absent, the run is blocked/failed transparently instead of pretending it analysed imagery.

## 5. UML Diagrams

Maintained Mermaid sources: [use case](diagrams/use-case.mmd), [activity](diagrams/activity.mmd), [sequence](diagrams/sequence.mmd), [component](diagrams/component.mmd), [deployment](diagrams/deployment.mmd), and [ER diagram](diagrams/er-diagram.mmd). The diagrams use Mermaid flowchart/sequence/ER grammar and were manually checked against routes/models. Mermaid CLI was not installed, so no rendered image files are claimed.

The use-case diagram separates the operator from CDSE and optional SMTP. The activity diagram makes the prerequisite branches visible. The sequence diagram shows `202 Accepted` being returned while the monitoring service continues in the background. The component/deployment diagrams distinguish local host components from external CDSE/SMTP. The ER diagram reflects actual principal SQLAlchemy models; minor helper/audit fields are deliberately omitted for slide readability.

## 6. Technologies and Tools Used

| Technology/tool | Actual use in SentinelWatch | Why it is used | Evidence in repository |
|---|---|---|---|
| Python | application and ML/training code | concise backend/scientific ecosystem | `app/`, `training/` |
| FastAPI + Uvicorn | HTTP app and API server | typed routes/OpenAPI/dev server | `app/main.py`, `requirements-core.txt` |
| Jinja2 + HTML/CSS/JS | single-page dashboard shell | lightweight UI without a frontend build | `templates/`, `static/` |
| Leaflet | map in AOI UI | browser mapping/drawing context | `index.html`, `app.js` |
| SQLite + SQLAlchemy | persistent local data | simple demonstrable relational storage | `database.py`, `models.py` |
| HTTPX | CDSE HTTP calls | async-friendly HTTP client | `satellite/copernicus.py` |
| Copernicus Data Space STAC | product metadata search | official Sentinel catalogue | `copernicus.py` |
| NumPy + Pillow | baseline array/image processing | portable numerical/image operations | `analysis.py`, core requirements |
| Rasterio/PyProj | optional spatial raster work | alignment, georeferencing and pixel area | operational requirements, `geospatial.py` |
| PyTorch | optional Siamese U-Net | learned paired-image model | `ml/`, ML requirements |
| ReportLab | optional case PDF | server-side printable report | `reporting.py` |
| pytest + Ruff | tests and lint | behaviour checking/code quality | `tests/`, dev requirements |

Why these choices: FastAPI makes APIs and OpenAPI documentation quick to expose; SQLite reduces setup for a final-year demo; SQLAlchemy gives structured persistence; STAC is a standard catalogue interface; NumPy enables a baseline even without a trained model; PyTorch keeps a credible upgrade path. “Listed in requirements” does not mean “active locally”: Rasterio/PyTorch/ReportLab are optional dependency groups and the current system status says Rasterio and model checkpoint are absent.

## 7. Hardware and Software Requirements

| Scenario | Practical requirement |
|---|---|
| Dashboard/API | 64-bit Windows/Linux/macOS, Python compatible with project dependencies, 4 GB RAM minimum, modern Chrome/Edge/Firefox, core requirements, local write permission for `instance/` and `storage/` |
| Real metadata search | internet access to CDSE STAC; no OAuth credential required for the observed metadata query |
| Imagery/inference | operational requirements, CDSE OAuth credentials, Rasterio/PyProj, disk space for GeoTIFFs; 8 GB RAM recommended |
| Model training | ML requirements, labelled lawful paired dataset, substantially more storage/RAM; GPU is optional but recommended for practical training, not required by source |

Use the exact dependency files: `requirements-core.txt`, `requirements-operational.txt`, `requirements-ml.txt`, and `requirements-dev.txt`. Sentinel-2 rasters can be large; keep the storage directory outside version control and budget tens of GB for repeated AOIs. Leaflet/CDN map tiles and CDSE calls need internet.

## 8. Algorithms and Methodology

**Candidate discovery.** `services/priority_discovery.py` grids a user region into 1–3 cells each dimension, searches catalogue availability, then uses `100 × (0.45 freshness + 0.35 cloud_quality + 0.20 availability)`. It ranks *where data is available*, not where illegal activity occurred. Mock-provider tests cover persistence/approval.

**Observation search/pairing.** `CopernicusDataSpaceProvider.search_acquisitions` sends a STAC search and parses product ID, date and cloud cover. The monitoring service chooses time-separated suitable records. Acquisition date is when the satellite observed the Earth; retrieval date is when the application downloaded/asked for the product. A catalogue is the external product index; SQLite is SentinelWatch’s local evidence database.

**Preprocessing and NDVI.** `geospatial.py` aligns/reprojects rasters, uses SCL/CLM masks and calculates NDVI when red/NIR bands exist:

`NDVI = (NIR − Red) / (NIR + Red)`

NDVI measures vegetation greenness but is vulnerable to cloud, shadow, season, sensor geometry and alignment differences. This code is present but unverified in the local runtime because Rasterio is absent.

**Baseline change detection.** `analyse_arrays` normalises illumination, computes RGB and vegetation/NDVI change probability, thresholds and morphologically cleans the mask, finds connected components, labels each region and converts pixel count using real pixel area. This is image differencing: hand-designed spectral rules, not learned classification. Controlled array tests exercise it.

**Optional Siamese U-Net.** `SiameseUNet` uses shared-encoder before/after inputs and produces a change-probability map. `inference.py` loads it only if a checkpoint is available. Training code uses paired dataset loading and BCE/Dice-style loss/evaluation utilities. No checkpoint, dataset, held-out metric, accuracy/IoU/F1, or live inference evidence exists; do not quote performance numbers.

**Alert priority/deduplication.** The alert service derives a heuristic score from confidence, persistence and area; it is not a calibrated probability. Alerts are created for configured thresholds and duplicate review records are avoided through case/finding state. SMTP attempts are always recorded, including `NOT_CONFIGURED`, so delivery is not falsely asserted.

## 9. Implementation Details

```
app/               FastAPI, routes, services, satellite and ML code
training/          dataset, training, evaluation and registration utilities
tests/             API/service/analysis contracts
instance/          local SQLite runtime database
storage/           generated previews/reports/runtime artifacts
```

`app/main.py` creates the app, static/template routes, lifecycle initialization and routers. Important endpoints include `POST/GET /api/areas`, `POST /api/areas/{id}/check-now`, `GET /api/system/status`, `POST /api/priority-candidates/discover`, `POST /api/alerts/{id}/assign`, `GET /api/data-status`, plus case/alert/report routes in the main app. The frontend uses `fetch` to call these endpoints and renders the returned JSON.

The core monitoring function is `execute_monitoring_run(run_id)` in `app/services/monitoring.py`; it persists status at each step. The implementation follows this shape:

```python
# app/services/monitoring.py (conceptual excerpt of actual flow)
acquisitions = provider.search_acquisitions(...)
pair = provider.select_pair(acquisitions, ...)
# only then, with credentials and Rasterio available:
analysis = analyse_arrays(before, after, ...)
```

Configuration is in `app/config.py` and `.env.example`: database/storage paths, CDSE URLs/OAuth, cloud/lookback/pixel controls, model location, scheduler, demo mode and SMTP. Never commit the real `.env`. `migrations.py` applies lightweight additive SQLite schema updates; it is useful for this project but is not a full production migration system such as Alembic.

To install and run from the repository:

```powershell
python -m pip install -r requirements-core.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Visit `http://127.0.0.1:8000/` and `http://127.0.0.1:8000/docs`. Add optional dependency sets only when their prerequisite is needed. Detailed repeatable steps are in the [Demo Guide](SentinelWatch_Demo_Guide.md).

## 10. Work Completed So Far

| Feature | Evidence found | Testing performed | Status | Remaining work |
|---|---|---|---|---|
| Dashboard/health | live page and `/api/health` | manually opened, API health | Implemented and tested | add real operational records |
| SQLite models/migration | models + database file | API/service tests | Implemented and tested | production migration strategy |
| CDSE metadata search | real adapter and a live 10-result query | live metadata query + mocks | Implemented and tested for metadata | retain proof after final setup |
| AOI/run API | routes/background workflow | API tests | Implemented and tested | live configured run |
| Baseline analysis | numpy code | controlled array tests | Implemented and tested | validate on labelled imagery |
| Raster/imagery path | source code | no local E2E | Implemented but not fully tested | credentials/Rasterio/live pair |
| ML model | architecture/training scripts | no checkpoint/evaluation | Partially implemented | dataset, train, register, validate |
| Alerts/routing | services/routes | tests with controlled state | Implemented and tested | real alert from satellite run |
| SMTP | code/status record | config-not-ready path | Implemented but not fully tested | SMTP configuration/delivery test |
| PDF report | ReportLab service/route | route contract test | Implemented but not fully tested | generate/open real PDF |
| Synthetic seed | `demo_seed.py` | not used in this run | Demo functionality | label clearly if used |

## 11. Current Project Status and Progress

**Verified now:** dashboard, health/status API, SQLite connection, primary API/service tests, Ruff, real STAC *metadata* retrieval. The local dashboard truthfully reports 0 AOIs, 0 observations, 0 analyses and 0 alerts; that is an empty real database, not a failure of the page.

**Waiting/blocked:** authenticated Process API imagery, Rasterio preprocessing, live change result, trained model, SMTP delivery and real PDF inspection. The database itself is connected and persists to the SQLite file. Scheduler is disabled. No major failing unit-test bug was found in this review, but live end-to-end status remains unverified.

**Priority list.** **P0:** configure lawful CDSE credentials and operational dependencies; create a real AOI and demonstrate a full metadata-to-pair run. **P1:** install Rasterio, obtain/cache a suitable cloud-limited image pair, validate preprocessing/baseline output, capture a genuine case/alert/PDF. **P2:** prepare labelled data, train/evaluate/register model and report measured metrics; configure/test SMTP. **P3:** durable worker queue, authentication/roles, PostGIS/object storage, model monitoring and a full migration framework.

No completion percentage is assigned because operational work is uneven: many code modules exist, but the critical live imagery/model path has not been verified.

## 12. Results and Screenshots of Implemented Modules

An actual live dashboard was inspected on 12 October 2026. It showed database connected, but **0** stored observations/areas/analyses/alerts; CDSE imagery credentials, Rasterio and validated model were explicitly unavailable. This is useful evidence of honest state reporting, not a satellite-analysis result. The screenshot-capture interface available during this review could display the live dashboard but did not export a repository image file; therefore `docs/screenshots/` is present but contains no invented image.

| Figure | Module | Evidence/caption | What to say |
|---|---|---|---|
| 1 | Main dashboard | Live page verified: health/status cards and configured limitations displayed | “This is the real dashboard. It shows connected SQLite but no fabricated observation or alert.” |
| 2 | Priority discovery | UI route exists; no live candidate is stored | “Candidates are preliminary catalogue-availability cells, not detected incidents.” |
| 3 | Change result | Not captured: no credentials/Rasterio/pair | “I will capture this only after a successful real run.” |
| 4 | Alert/department/report | Not captured: no real finding/case | “The API and tests exist; the operational artifact still needs verification.” |

Actual safe response evidence: `GET /api/health` returned `{"status":"ok","service":"SentinelWatch"}`. A live CDSE metadata search over a small Bengaluru-area bbox with 365-day lookback and cloud ≤80 returned 10 products; newest recorded result was `S2B_MSIL2A_20261009T050719_N0513_R019_T43PGQ_20261009T090953` acquired `2026-10-09T05:07:19.024000`, cloud `57.33`. This confirms catalogue metadata access only, not image download or detection.

## 13. Testing and Results

| Test ID | Component | Scenario | Expected result | Actual result | Status |
|---|---|---|---|---|---|
| T01 | API health/dashboard | app starts and health route responds | 200 service status | 200 `SentinelWatch` | Passed |
| T02 | Analysis | controlled before/after arrays | identify synthetic change safely | suite passed | Passed |
| T03 | Provider | STAC parsing/pair selection with mocks | correct metadata/pair logic | suite passed | Passed |
| T04 | Monitoring service | metadata persistence and truthful blocked states | status/evidence persisted | suite passed | Passed |
| T05 | Operations | candidate/department/assignment APIs | stored outputs and notifications | suite passed | Passed |
| T06 | Reporting | report endpoint contract | expected response/fallback contract | suite passed | Passed |
| T07 | Lint | `ruff check .` | no lint errors | “All checks passed” | Passed |
| T08 | Live CDSE metadata | public STAC query | products returned | 10 products returned | Passed |
| T09 | Live imagery-to-alert | credentials/raster/model workflow | real case/alert | Not run; prerequisites missing | Not run |
| T10 | SMTP/PDF artifact | external delivery/open generated PDF | verifiable delivery/artifact | Not run | Not run |

Command results were `19 passed, 103 warnings in 5.82s` and `All checks passed!`. The warnings were deprecation warnings from dependencies, not failing tests. A unit test proves an isolated behaviour; it does not prove CDSE retrieval, raster alignment, model quality or email delivery in production.

## 14. Future Work and Remaining Tasks

**Incomplete before final submission:** install operational dependencies; configure CDSE OAuth; demonstrate one lawful AOI with a suitable pair; inspect the generated analysis/case/alert/report; capture the missing real screenshots. Train and hold-out evaluate the Siamese U-Net on a documented lawful dataset before enabling it. Report only measured metrics.

**Improvements:** better cloud/shadow/seasonality controls, parameter calibration against labelled data, explicit human verification workflow, SMTP configuration, scheduled worker with durable queue, authentication/audit permissions, and a stronger migration strategy.

**Long-term:** PostGIS/object storage, multiple satellite sources, change-history timelines, model drift evaluation, field-validation feedback loop, and scalable deployment. None of these should be presented as currently complete.

## 15. Conclusion

SentinelWatch addresses the practical challenge of repeatedly screening remote areas by combining an AOI dashboard, official Sentinel-2 catalogue search, local evidence persistence and a proposed raster/model analysis pipeline. The verified capabilities are the web/API/data foundation, metadata search, baseline-analysis code contracts, alert workflow contracts and transparent health/model status. Its value is prioritizing human investigation, not declaring illegality. The main next step is a fully configured, documented real image-pair run followed by honest validation of the baseline and—only later—the trained model.
