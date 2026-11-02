# SentinelWatch v3 — Engineering Contract

Shared interface between the ML, backend and frontend workstreams. Change this file first if an
interface has to move, and keep every consumer in step.

## 1. Product behaviour

An operator marks places in India. SentinelWatch re-checks each one on a schedule (6 h, 12 h, 24 h or
weekly), analyses every new usable Sentinel-2 scene, works out what kind of change occurred and why,
and immediately dispatches an alert to the responsible government department.

**Revisit reality.** Sentinel-2 images a location roughly every 2-5 days. The schedule is how often
the catalogue is polled. A new image exists only when the satellites have passed and the scene is not
cloudy. The UI states this; it never invents a scene.

**Truthfulness rules (non-negotiable).**
- Every image and metric is real. No synthetic imagery, no hard-coded scores, no faked progress.
- A satellite cannot establish legality. The strongest verdict is `SUSPECTED_UNAUTHORISED`, meaning
  physical change was found and no authorisation is on record. Copy says "suspected", never "illegal
  activity confirmed".
- Results from past imagery carry `origin = "archive_replay"` and are visibly badged. Live results
  carry `origin = "live"`.
- Seeded department mailboxes use the reserved `.example` domain. Never seed a real government address.

**Current implementation qualifications.** Alert rows group eligible regions by category and verdict; repeated detections merge only with the same category/verdict and origin, and replay merges stay within one replay ID. Repeating a replay may add replay alerts. Remote imagery operations have a hard fetch deadline and per-band retry. Cached evidence remains available offline; new catalogue searches and uncached windows need network access. An illustrative-zone review buffer marks boundary uncertainty without extending authorisation. The short-pair evidence rule can miss real change when later pixels still look vegetated. Same-season references use an earlier year near the baseline period for multi-year pairs; always read the recorded date. Map imagery deliberately has no third-party boundary or label layer.

## 2. Bands and arrays

All model and analysis arrays use this band order on one 10 m grid, as float32 surface reflectance
in `[0, 1]`, shaped `[6, H, W]`. Radiometry follows `app/satellite/stac.py`: scale digital
numbers after removing only the offset still present in the provider's pixels. Earth Search
COGs with `earthsearch:boa_offset_applied=true` already have the BOA offset removed; do not
apply the STAC offset again. Pre-04.00 baselines have no offset. For modern Earth Search
scenes with a false or absent flag, radiometry is ambiguous and the provider chain reads
the raw scene from a fallback provider (Planetary Computer removes 1000 DN for baseline
04.00 or later). Zero DN remains no-data.

```
index  0    1    2    3    4    5
band   B02  B03  B04  B08  B11  B12     (B11/B12 bilinearly upsampled from 20 m)
```

`valid` is a bool `H x W` array: False for no-data, cloud, cirrus and cloud shadow (SCL 0,1,3,8,9,10).

## 3. ML interface (`app/ml/inference.py`)

```python
class ChangeDetector:
    def __init__(self, checkpoint: Path | None = None): ...
    available: bool; device: str; threshold: float      # threshold tuned on validation data
    def predict(self, before: np.ndarray, after: np.ndarray, valid: np.ndarray) -> np.ndarray:
        """[6,H,W] x 2 -> float32 H x W change probability in [0,1]. Sliding window, any size."""

LANDCOVER_NODATA = 255  # invalid pixels; never an index into LANDCOVER_CLASSES
LANDCOVER_CLASSES = ("tree", "shrub_grass", "crop", "built", "bare", "water", "wetland", "mine")

class LandCoverClassifier:
    def __init__(self, checkpoint: Path | None = None): ...
    available: bool; device: str
    def predict(self, scene: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """[6,H,W] -> (uint8 H x W class index, float32 [8,H,W] probabilities).
        Invalid/non-finite pixels have label LANDCOVER_NODATA and all-zero probabilities.
        Consumers exclude these labels before voting or indexing LANDCOVER_CLASSES.
        """

def get_change_detector() -> ChangeDetector: ...          # process-wide cached singletons
def get_landcover_classifier() -> LandCoverClassifier: ...  # safe for concurrent threads
```

Files: `models/checkpoints/change_siamese.pt`, `models/checkpoints/landcover_unet.pt`,
`models/registry.json`. Evaluation artefacts are committed under `reports/ml/`.

`models/registry.json` is the single source for model facts shown in the UI:

```json
{"generated_at": "ISO", "models": [{
  "key": "change|landcover", "name": "...", "version": "...", "architecture": "...",
  "parameters": 0, "status": "READY", "checkpoint": "models/checkpoints/x.pt",
  "checkpoint_sha256": "...", "input_bands": ["B02"], "trained_at": "ISO", "device": "mps",
  "epochs": 0, "training_seconds": 0, "threshold": 0.5,
  "datasets": [{"name": "...", "role": "train|val|test", "samples": 0, "url": "...", "licence": "..."}],
  "metrics": {"split": "test", "f1": 0, "iou": 0, "precision": 0, "recall": 0, "accuracy": 0,
               "per_class": {"tree": {"f1": 0, "iou": 0}}, "per_dataset": {"oscd_test": {"f1": 0}}},
  "artifacts": {"learning_curve": "reports/ml/x.png", "confusion_matrix": "reports/ml/y.png",
                "samples": "reports/ml/z.png"},
  "limitations": ["..."]
}]}
```

## 4. Classification vocabulary

`Finding.category`:
`DEFORESTATION`, `MINING`, `CONSTRUCTION`, `URBANISATION`, `FLOOD`, `LANDSLIDE`, `BURN_SCAR`,
`SEASONAL_VEGETATION`, `CROP_CYCLE`, `WATER_CHANGE`, `OTHER_CHANGE`.

`Finding.verdict`:
| value | meaning | alert behaviour |
|---|---|---|
| `SUSPECTED_UNAUTHORISED` | human-caused change, no authorisation on record | Dispatch immediately, high priority |
| `AUTHORISED_ACTIVITY` | inside a registered authorised zone of a matching type | Logged; low-priority notice |
| `NATURAL_EVENT` | flood, landslide or fire pattern, ideally with weather/disaster corroboration | Dispatch to disaster authority |
| `SEASONAL_BENIGN` | same place looks normal against the same season last year, or a crop cycle | No alert; retained as evidence |
| `NEEDS_REVIEW` | material change, cause undetermined | Dispatch, medium priority |

Every finding stores `verdict_reasons: list[str]`, plain sentences a viva examiner can read (e.g.
"Same-season comparison with 2025-11-03 still shows this change, so it is not seasonal.").

Department routing:
`DEFORESTATION`, `BURN_SCAR` → Forest Department; `MINING` → Mines & Geology; `CONSTRUCTION`,
`URBANISATION` → Town & Country Planning; `FLOOD`, `LANDSLIDE` → State Disaster Management Authority;
anything else → Environment / Pollution Control Board.

Alert lifecycle `Alert.status`:
`open` → `dispatched` → `acknowledged` → `field_verification` → `resolved` | `false_positive`.

## 5. HTTP API

All JSON. Datetimes are UTC ISO-8601 with a trailing `Z`. Image fields are URLs servable as given.
The existing routes stay available unless replaced here.

```
GET  /api/health                     {status, service, version, uptime_seconds}
GET  /api/overview                   see below
GET  /api/events/stream              Server-Sent Events, see below

GET/POST /api/areas                  POST body also accepts state, district, site_type, description
GET/PATCH/DELETE /api/areas/{id}
POST /api/areas/{id}/check-now       202 -> run
POST /api/areas/{id}/pause|resume
POST /api/areas/{id}/replay          {start_date, end_date, max_scenes} -> 202 {replay_id, run_ids}
GET  /api/areas/{id}/timeline        [{date, product_id, cloud_cover, aoi_clear_fraction, status,
                                       thumbnail, case_id, categories[], satellite}]
GET  /api/monitoring-runs/{id}       run

GET  /api/alerts?status=&severity=&category=&department_id=&area_id=&limit=
GET  /api/alerts/{id}                alert + case summary + dispatches
PATCH /api/alerts/{id}/status        {status, note, actor}
POST /api/alerts/{id}/dispatch       {department_id?} manual re-dispatch

GET  /api/cases?limit=&area_id=      case summaries
GET  /api/cases/{id}                 case detail
GET  /api/cases/{id}/report          application/pdf
PATCH /api/cases/{id}/status

GET/POST /api/departments
GET  /api/departments/{id}/inbox     alerts routed to this department, with dispatches
GET  /api/dispatches?limit=          outbound log
GET/POST/DELETE /api/authorised-zones[/{id}]
    {id, name, zone_type: MINING|CONSTRUCTION|FORESTRY|OTHER, geometry, authority, reference,
     valid_from, valid_to, source, is_illustrative}

GET  /api/models                     registry.json models, each with absolute artifact URLs
GET  /api/system/status              {provider, providers[], scheduler{}, model_status, device, smtp, ...}
GET  /api/data-status                existing
```

`GET /api/overview`:
```json
{"generated_at": "...",
 "counts": {"areas": 0, "active_areas": 0, "open_alerts": 0, "critical_alerts": 0, "cases": 0,
            "scenes_analysed": 0, "changed_area_m2": 0, "alerts_24h": 0, "dispatches_sent": 0},
 "scheduler": {"enabled": true, "last_tick_at": "...", "next_due_at": "...", "active_runs": 0,
               "queued_runs": 0, "workers": 3},
 "provider": {"name": "...", "healthy": true, "last_scene_at": "...", "last_error": null},
 "models": {"change": {"status": "READY", "f1": 0}, "landcover": {"status": "READY", "f1": 0}},
 "by_category": {"MINING": 0}, "by_verdict": {"NEEDS_REVIEW": 0},
 "activity": [{"at": "...", "type": "run|alert|dispatch", "text": "...", "area_id": "..."}]}
```

Area payload adds: `state, district, site_type, description, next_check_at, open_alerts,
last_scene {date, product_id, thumbnail, aoi_clear_fraction}, health: OK|CLOUDED|ALERT|ERROR|PAUSED`.

Case detail adds: `origin, area {id,name,state,bbox}, image_bounds [[s,w],[n,e]],
images {before, after, overlay, ndvi_before, ndvi_after, ndvi_change, panel},
seasonal_reference {date, product_id, image}, context {rainfall{}, disasters[], protected_areas[],
osm_features[], authorised_zones[]}, findings[]`.

Finding adds: `verdict, verdict_reasons[], transition {from, to}, indices {ndvi_delta, nbr_delta,
ndwi_delta, bsi_delta, ndbi_delta}, change_probability, seasonal_persists (bool|null), geometry`
(GeoJSON WGS84).

Alert adds: `category, verdict, origin, area_id, area_name, department {id,name}, dispatched_at,
detected_at, acquisition_date, thumbnail, confidence, area_m2, priority_score,
latency_seconds` (scene processed → dispatched).

### SSE events (`/api/events/stream`)

`event:` name with a JSON `data:` line. Send a `heartbeat` every 15 s and honour reconnection.

| event | data |
|---|---|
| `heartbeat` | `{at, active_runs, queued_runs}` |
| `run` | `{run_id, area_id, area_name, status, stage, progress_percent, message}` |
| `alert` | alert payload (section 5) |
| `dispatch` | `{alert_id, department, channel, status, destination, at}` |
| `scene` | `{area_id, area_name, date, product_id, aoi_clear_fraction}` |

## 6. Ownership

| Paths | Owner |
|---|---|
| `app/ml/`, `training/`, `data_tools/`, `models/`, `reports/ml/`, `requirements-ml.txt` | ML |
| `app/*.py`, `app/api/`, `app/services/`, `app/satellite/`, `tests/`, `requirements-core.txt`, `requirements-operational.txt`, `.env.example`, `Dockerfile`, `docker-compose.yml`, `scripts/` | Backend |
| `app/static/`, `app/templates/` | Frontend |
| `README.md`, `docs/`, `RELEASE_NOTES.md`, `CHANGELOG.md` | Integrator |

Agents share one working tree. Stage only explicit paths you own, commit small and often with
`git add <paths> && git commit`, and push to `origin feature/complete-sentinelwatch`. Never use
`git add -A`, `git stash`, `git reset`, `git checkout -- <path>` or `git rebase`. If the index is locked, wait and retry. If a push is
rejected, run `git pull --no-rebase --no-edit` and push again. Never change git identity or credential
config, which is already set for this repository.
