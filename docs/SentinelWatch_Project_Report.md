# SentinelWatch: Satellite Land-Change Monitoring and Department Alerting

Final-year engineering project report · v3.0.0 · Documentation inspection: 2 November 2026

**Evidence statement.** This report describes inspected source in `app/`, `training/` and `data_tools/`, not a claim that every integration has run successfully. Backend, frontend and ML work share the checkout and are changing concurrently. Training is still running at this handoff. Model measurements remain explicit tokens for the integrator to fill from `models/registry.json` and its matching reports. Site outcomes remain result tokens until their run/case evidence is inspected. No training, PyTorch import, extra server or full application test suite was run by the documentation worker.

**Submission PDF.** A new PDF must be built from this report after the integrator reconciles the final code, measurements and screenshots. `output/pdf/SentinelWatch_Final_Year_Project_Review_Package.pdf` is obsolete and describes an earlier system. At the user's direction, deletion of that file is left to the integrator because it is outside this worker's owned paths. No replacement PDF is built here. This report replaces `SentinelWatch_Project_Review.md`.

## Abstract

SentinelWatch helps an operator monitor marked places in India for physical land change using real Sentinel-2 imagery. The system periodically searches an imagery catalogue, checks usable pixels within each monitored area, selects comparable dates and processes six spectral bands on a common grid. A shared-encoder Siamese U-Net can supply change evidence and a single-date U-Net can supply land-cover classes. Explicitly labelled spectral methods remain available if learned inference is unavailable. Connected change regions are interpreted using land-cover transitions, spectral indices, a same-season reference, rainfall, disaster feeds, terrain and mapped context. A local authorised-zone registry supports a separate permission-record check. Eligible findings become department alerts with evidence, plain-language reasons, delivery records and a human review lifecycle. Historical processing is labelled Archive replay. The contribution is an inspectable evidence-to-review workflow. The system does not establish illegality, and this report does not claim unmeasured model quality or unverified deployment results.

## 1. Problem statement

Repeated field inspection of dispersed forest, mining, settlement and river sites is difficult. A two-date image difference alone cannot distinguish clearance from harvest, leaf-fall, flood, shadow or a legitimate project. A useful monitoring tool must preserve where the observation came from, explain why it was flagged, record missing evidence, and route the lead to an appropriate reviewer.

The project asks: can a small web application turn real satellite observations into traceable, prioritised land-change leads while keeping seasonal interpretation, permission records and human judgement separate?

The intended users are an operator and departmental reviewers. Seeded inboxes represent routing destinations; no connection to a government case-management system or official permission database is claimed.

## 2. Objectives, scope and requirements

- Let an operator mark a place, choose categories and schedule catalogue checks every 6 h, 12 h, 24 h or weekly.
- Retrieve real L2A imagery through public providers, with failure states and cached windows rather than invented scenes.
- Detect candidate change, estimate land-cover transition, and explain a proposed category and verdict.
- Test recurrence against the same season last year and check recorded authorisation independently.
- Route eligible alerts, retain delivery outcomes, and support acknowledgement, field verification, resolution and false-positive review.
- Preserve acquisition IDs, processing method, masks, geometry, context, reasons and review history.
- Report held-out evaluation only from matching model artifacts; label Archive replay and illustrative permit records.

The scope is a final-year decision-support system for bounded areas. It is not continuous video, individual identification, automatic enforcement or a comprehensive legal-clearance service. A schedule polls for available scenes. Sentinel-2 revisits roughly every 2–5 days depending on coverage; the official mission description specifies a five-day equatorial constellation revisit [8]. Clouds and catalogue delays can make usable observations less frequent. A new live scene may not arrive during a demonstration.

## 3. Literature survey

References below were checked through publisher, author, institutional or official service pages during documentation. Published findings are background, not SentinelWatch results.

| Work | Relevant contribution | Application and limitation here |
|---|---|---|
| Daudt, Le Saux and Boulch, ICIP 2018 [1] | Fully convolutional change detection with early fusion and Siamese alternatives | Motivates shared features and dense change masks; this repository implements an FC-Siam-diff-style network, not a claim of identical reproduction |
| Daudt, Le Saux, Boulch and Gousseau, IGARSS 2018 [2] | Introduces OSCD urban-change benchmark and multispectral CNN comparison | Supports paired supervised learning; L1C urban data do not establish performance across Indian rural/hazard classes |
| Zanaga et al., ESA WorldCover 2021 v200 [3] | Global land-cover reference derived from Sentinel observations | Supplies mapped land-cover labels; agreement with an existing model-derived map is not independent field truth |
| Hansen et al., Science 2013 [4] | Maps tree-cover loss and gain with Landsat time series | Motivates annual forest-loss supervision; coarse annual labels cannot date individual changes within the satellite pair |
| Maus et al., 2022 [5] | Global mining polygons and a documented footprint dataset | Supplies mining-footprint supervision; mapped pits, plants, ponds and tailings are neither operating status nor permission |
| Global Forest Watch integrated alerts [6] | Combines complementary optical and radar disturbance alerts | Motivates corroboration and alert triage; SentinelWatch does not implement RADD or ingest this integrated layer |
| India's Mining Surveillance System [7] | Satellite-derived triggers sent to State Governments for field verification | Motivates the distinction between trigger and verified outcome; SentinelWatch is an independent student project with no MSS integration |

The GFW source now describes integrated **disturbance** alerts incorporating GLAD-L, GLAD-S2, RADD and DIST-ALERT; older descriptions call the layer integrated deforestation alerts [6]. The useful lesson is complementary observations and verification, not a claim that an optical-only project can see through clouds. WorldCover cautions that differences between its annual product versions can include algorithm changes [9]; this project therefore uses the declared reference product rather than treating a product-version difference as measured land change.

## 4. System design

### 4.1 Architecture

The browser loads a Jinja-served page with JavaScript hash routes and locally vendored Leaflet assets. JSON calls go to FastAPI. SQLAlchemy persists evidence/workflow objects in SQLite with WAL, foreign keys and a busy timeout. A bounded thread pool executes monitoring runs; a polling thread queues due active sites. The event bus publishes run, scene, alert, dispatch and heartbeat Server-Sent Events; the frontend also polls to recover state.

The provider chain starts with Earth Search, then tries Planetary Computer on failure, and includes CDSE when configured. Rasterio reads cropped COG windows onto an AOI grid; the chain caches SCL and band windows. Analysis writes immutable run-specific evidence. Alerting deduplicates and routes findings. Reporting builds a case investigation PDF; dispatch records in-app routing and attempts SMTP or saves an unsent email.

Sources: [architecture](diagrams/architecture.mmd), [components](diagrams/component.mmd), [deployment](diagrams/deployment.mmd), [DFD 0](diagrams/dfd-level-0.mmd), [DFD 1](diagrams/dfd-level-1.mmd), [DFD 2](diagrams/dfd-level-2.mmd).

### 4.2 Modules and source responsibilities

| Module | Source | Responsibility |
|---|---|---|
| Web shell/views | `app/static/js/`, `app/templates/index.html` | Operations, site/timeline/replay, incident evidence, filters, inboxes, zones and models |
| API and validation | `app/api/monitoring.py`, `operations.py`, `schemas.py` | AOI CRUD, queued checks, replay, evidence/review and registry endpoints |
| Scheduler/events | `app/services/scheduler.py`, `events.py` | Per-AOI serialization, bounded workers, recovery/backoff and SSE |
| Providers/grid | `app/satellite/chain.py`, `stac.py`, `copernicus.py`, `grid.py` | STAC discovery, equivalent-overpass failover, cache, reflectance/grid handling |
| Orchestration | `app/services/monitoring.py` | Pair/reference selection, inference/fallbacks, context, persistence and dispatch |
| Region analysis | `app/services/analysis.py` | Indices, fusion, morphology, cause rules, seasonality and authorisation |
| Context | `app/services/context.py` | Cached rainfall, disaster feeds, OSM features and terrain; failure recorded independently |
| Learned inference | `app/ml/inference.py`, `preprocessing.py`, network modules | Lazy checkpoint loading, shared preprocessing, sliding windows and invalid-pixel handling |
| Alerts/dispatch | `app/services/alerting.py`, `dispatch.py`, `vocabulary.py` | Eligibility, overlap merging, state routing and channel outcomes |
| Evidence/reporting | `app/services/evidence.py`, `reporting.py`, `payloads.py` | Images, geographic evidence, PDF bytes and API presentation |
| Data/training | `data_tools/`, `training/` | Real data preparation, geographic splits, optimisation, held-out reports and registry |
| Persistence/security | `models.py`, `database.py`, `migrations.py`, `auth.py` | SQLite evidence trail, additive schema handling and optional write login |

### 4.3 API and user flows

The actual source routes, not diagram labels, define the interface. `/api/health`, `/overview`, `/system/status`, `/data-status` and `/models` expose operational/model facts. `/api/areas` supports creation/listing, with detail/update/delete, pause/resume, `check-now`, `replay`, `timeline`, acquisitions and analyses under the area ID. A check returns a queued run with HTTP 202; conflicting in-flight checks return 409. `/api/monitoring-runs/{id}` exposes progress/outcome.

Alerts support filtering, detail, status changes, manual dispatch, notifications and compatibility acknowledge/assign routes. Cases support detail/listing, status/review actions and `/api/cases/{id}/report`. Departments expose creation/listing and an inbox. Authorised zones support create/list/detail/delete. Priority-candidate discovery/approval remains a compatibility workflow for catalogue availability, not a detector output. Authentication has login/logout/me routes when configured.

The standard alert path is `open → dispatched → acknowledged → field_verification → resolved` or `false_positive`. The transition table in `app/vocabulary.py` also permits early closure and selected shortcuts; it does not require every intermediate step. Case review statuses are a separate vocabulary. See [use cases](diagrams/use-case.mmd), [sequence](diagrams/sequence.mmd) and [activity](diagrams/activity.mmd).

## 5. Imagery and context sources

| Source | Role and inspected behaviour | Interpretation boundary |
|---|---|---|
| Earth Search | First public STAC/COG provider | Real pixels still depend on provider and network availability |
| Planetary Computer | Public fallback; signs asset URLs without a user's credentials | Signed URL expiry and outages require retry/failover |
| CDSE | Additional provider only when its credentials are supplied | Not the mandatory primary imagery source |
| Sentinel-2 L2A SCL | AOI usability and pixel masking | Residual haze, cloud edges and SCL mistakes remain possible |
| Open-Meteo | Rainfall around acquisition dates and prior-year context | Gridded estimates do not prove a local cause |
| NASA EONET / GDACS | Nearby reported disasters | Sparse feeds and temporal/spatial proximity are corroboration, not event attribution |
| OSM / Overpass | Quarry and protected-area overlap | Community mapping is incomplete and is never permission |
| Copernicus DEM | Terrain slope requested when the landslide rule needs it | Coarser terrain and DSM effects limit local slope precision |
| Local authorised zones | Geometry, type, validity, authority/reference/source and illustrative flag | Not a complete or authenticated official permit registry |

Provider identity, product ID, acquisition time and method labels belong to the evidence. A provider health flag before a request is not a guarantee that a live network operation will succeed.

## 6. Algorithms

### 6.1 Scene selection and preprocessing

1. Construct a snapped 10 m UTM grid for the AOI. Enforce the configured maximum side; larger areas must be split.
2. Search a lookback interval and persist catalogue records, provider-independent scene keys and AOI links. Group products by date and consider later scenes newest-first.
3. Measure clear fraction **inside the AOI** using SCL. Invalid labels are `0, 1, 3, 8, 9, 10` (no data/defective, shadow, cloud and cirrus). Tile-wide cloud metadata alone is insufficient.
4. Choose a new usable later scene, then a clear baseline at least the configured gap earlier with enough shared valid coverage. If the normal lookback has no baseline, the current code attempts an extended baseline search for long cloudy periods. Defaults are a 20-day minimum gap, 0.60 clear fraction and 0.30 pair coverage. These are code settings, not measured performance.
5. Search near the later date minus 365 days for a usable seasonal reference; the configurable window defaults to ±45 days. Missing/insufficient reference is recorded as untested.
6. Read `B02, B03, B04, B08, B11, B12` as float32 reflectance in `[0,1]`, shape `[6,H,W]`. Apply provider scale/offset logic. Bilinearly resample continuous bands, including SWIR from 20 m; use nearest neighbour for SCL. Upsampling does not create finer SWIR information.
7. Intersect valid masks and buffer invalid edges by three pixels before comparison. Persist quality and no-pair/no-new-scene outcomes.

The implementation uses a finite SCL-read budget and processes the newest eligible date first. It does not prove that every historical observation was analysed. Archive replay samples selected real dates over a requested past interval and submits separate serial runs; it is not a synthetic-data mode. Starting replay still requires catalogue access; already queued replay can reuse known metadata/cached imagery where available. Neither cache nor snapshot should be described as a new live acquisition.

### 6.2 Change and land-cover networks

`SiameseUNet` uses one shared encoder for both dates. Absolute feature differences at multiple scales feed a decoder producing one change logit per pixel. This makes the feature comparison symmetric; direction comes later from before/after land-cover labels and index deltas. The architecture follows the FC-Siam-diff idea [1] with repository-specific blocks and preprocessing.

The change model uses per-band median/IQR statistics pooled across both dates. Pooling preserves inter-date contrast better than independently centring each date. Scale floors and clipping limit amplification in uniform scenes. This reduces first-order L1C/L2A radiometric differences; it does not prove domain invariance.

`LandCoverUNet` is a single-date encoder-decoder with eight classes: `tree`, `shrub_grass`, `crop`, `built`, `bare`, `water`, `wetland`, `mine`. It uses training-set band statistics stored with its checkpoint. Overlapping inference windows are blended; invalid land-cover pixels have class 255, and invalid change pixels receive zero. Concurrent inference is guarded by locks and cached model instances.

A missing, invalid or failing model has a labelled fallback. Change analysis then uses spectral indices; land-cover rules provide coarse classes. The land-cover fallback cannot recognise a mine directly and only crudely distinguishes vegetation classes. A registry status of `READY` depends on matching evaluation evidence; availability at inference time only means the checkpoint can load. Do not confuse these conditions.

### 6.3 Spectral evidence and regions

For bands on the common grid:

- `NDVI = (B08 − B04)/(B08 + B04)` for vegetation.
- `NBR = (B08 − B12)/(B08 + B12)` for burn-related contrast.
- `NDWI = (B03 − B08)/(B03 + B08)` for water contrast.
- `BSI = ((B11 + B04) − (B08 + B02))/((B11 + B04) + (B08 + B02))` for exposed soil.
- `NDBI = (B11 − B08)/(B11 + B08)` for built/bare-surface evidence.

Safe division handles near-zero denominators. For each index the baseline subtracts the AOI median inter-date shift, scales its absolute residual against a sensitivity-adjusted threshold, takes the strongest index and smooths it. Median removal suppresses broad radiometric shifts but can also suppress truly widespread changes; pooled neural preprocessing does not remove that limitation of the spectral gate.

With a model, probabilities are rescaled so the checkpoint's validation-selected threshold maps to 0.5. The code blends model evidence with spectral evidence (weights 0.6 and 0.4), thresholds the fused map at 0.5, and also requires spectral support of at least 0.30. These are design constants, not evaluation scores. Without the model, the spectral map is thresholded alone. Binary opening/closing suppress small noise, eight-connected components form regions, and minimum-area/count limits remove small or excess components. Area equals retained pixels times the projected grid pixel area.

### 6.4 Cause rules: order matters

This table follows `_classify_cause` in `app/services/analysis.py`. Deltas mean later minus earlier. Constants are inspected rule settings, not measured results. The first matching rule wins.

| Priority | Evidence condition | Proposed category / qualification |
|---|---|---|
| 1 | New water/wetland or NDWI rise ≥0.25 with positive later NDWI | `FLOOD` if heavy rainfall or a flood feed corroborates; otherwise `WATER_CHANGE` |
| 2 | Water/wetland recedes to another known class | `WATER_CHANGE` |
| 3 | Transition into mine class; or BSI rise ≥0.15 with mine class / mapped quarry overlap ≥0.30 | `MINING`; footprint evidence does not prove extraction or permission |
| 4 | NBR drop ≥0.27, earlier class not crop, and dark later visible/NIR surface or nearby fire report | `BURN_SCAR`; ignition cause remains unknown |
| 5 | Vegetation becomes bare/shrub, extreme rainfall or landslide report, available mean slope ≥18° and scar bounding-box fill ≤0.55 | `LANDSLIDE`; missing terrain/context prevents this rule from firing |
| 6 | Tree becomes bare or shrub/grass | `DEFORESTATION`, subject to the subsequent seasonal test |
| 7 | Becomes built, or NDBI rise ≥0.15 with vegetation loss under the coded class restrictions | `URBANISATION` if at least 0.25 of the surrounding ring was built; otherwise `CONSTRUCTION` |
| 8 | Crop↔bare/shrub or crop change matching the coded cycle conditions | `CROP_CYCLE`; crop-to-built is handled earlier |
| 9 | Exposed soil without supporting cause, same-cover variation, or unmatched transition | `OTHER_CHANGE` and review rather than a specific accusation |

The burn darkness limits are visible reflectance ≤0.13 and NIR ≤0.26. Rainfall defaults are 100 mm (heavy) and 200 mm (extreme) over a 14-day window, including the wettest such window in the comparison interval when available. They are heuristic settings, not universal scientific/legal definitions. Every finding retains its index changes and readable reasons.

### 6.5 Same-season test

Let `R` be a region, `Vref` the valid reference pixels, and `I` an index. The test first requires `|R ∩ Vref|/|R| ≥ 0.5`; otherwise `seasonal_persists = null`. It considers index drivers whose regional before-to-after mean change magnitude is at least `0.6 × index_threshold`.

For a driver, subtract the AOI-wide median from the later-minus-reference difference on shared usable pixels. A reference-covered region pixel supports persistence when that residual has the original change direction and magnitude at least `max(0.5 × |regional_change|, 0.5 × index_threshold)`. Take the largest supporting fraction across drivers. Persistence is true when that fraction is at least 0.5. If no driver qualifies, compare the maximum normalised absolute index distance against 0.5 instead.

If persistence is false, the verdict becomes `SEASONAL_BENIGN`. The category becomes `CROP_CYCLE` when crop is involved or `SEASONAL_VEGETATION` for other non-water recurrence. Recurring water remains `WATER_CHANGE`. If a crop-cycle rule already matched, it is benign even if the reference is missing or persistence is true. Therefore this is a screening heuristic; an unusual harmful crop event could be suppressed. Missing reference does not prove either seasonality or permanence. Year-to-year hydrology and agricultural timing remain confounders.

### 6.6 Authorisation test and verdict order

Seasonal recurrence is considered first. For a remaining human-caused category, active registered zone geometries are rasterised on the analysis grid. A zone is active on the **later acquisition date**, with inclusive date limits where supplied. The first matching type covering at least half the region yields `AUTHORISED_ACTIVITY`.

| Registry type | Categories it can match |
|---|---|
| `MINING` | `MINING` |
| `CONSTRUCTION` | `CONSTRUCTION`, `URBANISATION` |
| `FORESTRY` | `DEFORESTATION` |
| `OTHER` | Any of those four human-caused categories |

The broad `OTHER` match and half-region overlap are implementation choices, not legal standards. Matching checks local records, not authenticity, conditions of a clearance, exact event date or full containment. Illustrative zones participate in this same check; the reason explicitly says they are not official permit records. An illustrative match cannot be presented as a real clearance.

With no matching record, the strongest verdict is `SUSPECTED_UNAUTHORISED`: **physical change consistent with a human activity, with no authorisation on record**. This can reflect registry incompleteness and requires field verification. Flood, landslide and burn-scar categories become `NATURAL_EVENT` when not overridden by recurrence. In particular, the label does not establish that a fire was naturally ignited. Unresolved material change becomes `NEEDS_REVIEW`.

### 6.7 Evidence scores, alerts and routing

Finding confidence is a weighted evidence-ranking heuristic: fused change support, rule strength and seasonal support. It is not calibrated class accuracy or a probability of wrongdoing. Priority additionally considers verdict and changed area. Severity follows verdict and area thresholds. None of these values constitutes a legal risk score.

`SEASONAL_BENIGN` findings are stored without alerts. Other findings must pass the configured confidence threshold. Site-selected categories restrict non-hazard categories; hazard categories bypass that category filter but still face the evidence threshold. Repeated same-category regions overlapping at least 0.20 of the smaller region merge into an active alert. Live and replay evidence do not merge; replay merging is restricted to the same replay ID. Merging updates persistence rather than automatically creating another dispatch.

| Category | Routing destination |
|---|---|
| Deforestation, burn scar | State Forest Department |
| Mining | State Mines & Geology Department |
| Construction, urbanisation | State Town & Country Planning Department |
| Flood, landslide | State Disaster Management Authority |
| Other eligible change | State Pollution Control Board |

New eligible alerts are routed immediately in the processing workflow, subject to queue/network/reporting time. `AUTHORISED_ACTIVITY` produces a low-priority in-app notice when alert eligibility is met; automatic email is skipped for it. Manual dispatch can request email. Other dispatched alerts create an in-app record and attempt SMTP when configured, otherwise save an `.eml` as `OUTBOX_SAVED`. Failures are recorded separately. A dispatch timestamp measures processing-to-routing latency, not time since the physical event and not confirmed delivery to an officer.

## 7. Database design

The [ER source](diagrams/er-diagram.mmd) uses the actual model class and column names from `app/models.py`. JSON fields are stored as text, not a spatial database type.

| Entity | Key data / links |
|---|---|
| `MonitoringArea` | Geometry/bbox, categories, state/district/type, interval/status, last/next check |
| `SatelliteAcquisition` | Unique product ID, scene key, provider, time, tile/level, source/assets/quality |
| `AOIAcquisition` | AOI and acquisition FKs; unique pair, clear fraction, scene status, thumbnail; `case_id` is a plain optional string |
| `MonitoringRun` | AOI FK; selected scene FKs, stage/status/progress, errors, origin/replay/as-of/trigger |
| `ModelVersion` | Model name/checkpoint, status and metrics metadata linked from analyses; distinct from the JSON presentation registry |
| `AnalysisRecord` | AOI/run/scene/model links, method, quality/result JSON, physical pixel area and evidence path |
| `Case` | Optional AOI/analysis/scene links, date pair, method, origin, images, seasonal reference/context and human-review state |
| `Finding` | Case FK, category/verdict/reasons, transition, indices, geometry, region area and seasonal result |
| `Alert` | Case FK; optional finding/department FKs, severity/status, origin/replay, detection/dispatch/outcome times; `aoi_id` is a plain string |
| `Department` | Name, routing category, state, recipient and active flag |
| `NotificationAttempt` | Alert FK; optional department FK, channel/destination/outcome, attempts/file/time |
| `CaseReview` | Case FK, action, reviewer label, note and time |
| `AuditLog` | Generic entity type/ID, action and details; no polymorphic foreign key |
| `CatalogueRefresh` | Optional AOI FK, query/outcome/count/newest scene and message |
| `PriorityCandidate` | Preliminary area/score/rationale; optional approved monitoring-area FK |
| `AuthorisedZone` | Standalone geometry/type/validity/source/reference/illustrative record; no finding-zone FK |

`MonitoringRun.analysis` and `AnalysisRecord.case` are modelled as optional single relationships, but their foreign-key columns are not declared unique. The ER diagram marks that ORM intent; uniqueness is not guaranteed by a database constraint. Finding-zone matches are copied into reasons/context, not represented by a relational join table. File evidence lives in storage and must be backed up consistently with SQLite. Lightweight additive migrations are not a full versioned migration framework.

## 8. Training and reproducibility

The runnable recipe and paths are in the [root README](../README.md#reproduce-training-and-evaluation). It was checked against argument definitions, not executed here.

**OSCD.** The fetcher uses a pinned archive mirror, path-checks ZIP members and caches six-band arrays without pickle. Official test cities stay test; predefined validation cities are removed from official training. OSCD urban annotations are not labels for all physical or natural change. Respect the change-map noncommercial academic licence.

**India forest.** The builder combines real L2A dry-season pairs with Hansen annual loss labels. Target-year loss is positive; stable forest with no recorded loss is negative; non-forest, other loss years and invalid pixels are ignored. Splits use geographic blocks with chip margins. Annual, coarser labels introduce spatial/temporal uncertainty. Scores on this data do not measure false alarms outside labelled forest.

**India land cover.** The builder samples L2A windows across states, mining belts and rare-class areas. It maps WorldCover classes into the application vocabulary and burns historical mining polygons into the mine class. Snow/moss and invalid observations are ignored. Held-out WorldCover tiles, pinned before training, have margins to reduce overlap. The label date and mining mosaic date are imperfectly aligned; a mining footprint can contain water, plant and tailings.

**Optimisation.** Change training uses weighted binary cross-entropy plus soft Dice with ignored pixels, random crops and paired geometric augmentation. Land cover uses class-weighted cross-entropy and rare-class sampling. Training uses validation to select checkpoints; the change threshold is tuned on validation. Test evaluation uses that frozen threshold and records checkpoint SHA-256, dataset/split, device and confusion counts. Repeating a report for the same checkpoint requires an explicit override. This guard does not make test-based model selection statistically valid: declare the selection protocol before consulting test results.

**Registry.** `training.build_registry` assembles model facts from installed checkpoints and measured reports. It matches test reports to checkpoint hashes and marks unevaluated checkpoints accordingly. Training/evaluation figures include learning curves, confusion matrices and sample panels. Cached singletons require restart after replacement. Random seeds improve repeatability but do not guarantee bitwise equality across devices; resumed sampling intentionally uses a different random stream.

## 9. Testing and verification

Documentation verification is deliberately lightweight on a machine already training models. No model was loaded by the documentation worker. No current full-suite result is inferred from an older report or another worker's logs.

| Evidence item | What was inspected or run | Status at handoff |
|---|---|---|
| Product vocabulary/API | Engineering contract, routers, request schemas, payload and routing code read | Source inspected; runtime flow pending integrator check |
| Database design | `models.py` columns/FKs/nullability read; ER consistency checked statically | Source-level consistency; database migration/runtime pending integrator check |
| Training recipe | Dataset builders and train/evaluate/registry argument definitions read | Commands not executed; training/evaluation pending integrator check |
| UI demo clicks | Hash routes, visible labels, incident and replay code read | Source-derived script; browser rehearsal pending integrator check |
| Bibliography | Author/publisher/official pages fetched or searched | Verified links in references; access limitations stated below |
| Markdown links and tokens | Lightweight static check recorded in completion checklist | See checklist for exact commands/results |
| Mermaid syntax | Parser check attempted as described in completion checklist | See checklist; rendering is separate from parsing |
| Application tests/lint | Relevant test sources inspected; no suite or lint command run in this task | Pending integrator check |
| Live imagery, SMTP and PDF output | No external operational run or dispatch performed in this task | Pending integrator check |
| Docker deployment | Docker unavailable on this machine | Unverified here; pending integrator check |

The test sources currently cover health/page serving and ML interface/preprocessing/security cases. Tests under active development must be reviewed again before attributing coverage. Unit tests using temporary/random fixtures exercise contracts; such fixtures must never become presentation satellite results. Integration verification should separately check real pair registration, cloudy/no-new-scene states, the seasonal and authorisation branches, channel outcomes and restart recovery.

## 10. Results and evaluation plan

### 10.1 Model results awaiting measured artifacts

These tokens are intentional. The integrator must fill them only after checking the installed checkpoint hash, split and registry/report correspondence. No table entry is a claimed result.

| Measure | Pending value |
|---|---|
| Change, OSCD test precision | {{METRIC:change_oscd_test_precision}} |
| Change, OSCD test recall | {{METRIC:change_oscd_test_recall}} |
| Change, OSCD test F1 | {{METRIC:change_oscd_test_f1}} |
| Change, OSCD test IoU | {{METRIC:change_oscd_test_iou}} |
| Change, OSCD test accuracy | {{METRIC:change_oscd_test_accuracy}} |
| Change, India forest test F1 | {{METRIC:change_india_forest_test_f1}} |
| Change, India forest test IoU | {{METRIC:change_india_forest_test_iou}} |
| Land cover, held-out India macro F1 | {{METRIC:landcover_india_test_macro_f1}} |
| Land cover, held-out India macro IoU | {{METRIC:landcover_india_test_macro_iou}} |
| Land cover, held-out India accuracy | {{METRIC:landcover_india_test_accuracy}} |
| Land cover, held-out mine-class F1 | {{METRIC:landcover_india_test_mine_f1}} |
| Change training / validation / test sample counts by dataset | {{METRIC:change_dataset_split_counts}} |
| Land-cover training / validation / test counts by tile | {{METRIC:landcover_dataset_split_counts}} |
| Installed change model training duration | {{METRIC:change_training_seconds}} |
| Installed land-cover model training duration | {{METRIC:landcover_training_seconds}} |
| CPU change inference duration and measurement conditions | {{METRIC:change_cpu_inference_seconds}} |
| CPU land-cover inference duration and measurement conditions | {{METRIC:landcover_cpu_inference_seconds}} |
| Peak memory and measurement conditions | {{METRIC:inference_peak_memory}} |

For binary change: precision is `TP/(TP+FP)`, recall is `TP/(TP+FN)`, F1 is `2TP/(2TP+FP+FN)`, and IoU is `TP/(TP+FP+FN)`. State which class is positive and how ignored pixels are excluded. Accuracy can be misleading when unchanged pixels dominate. The evaluator pools binary pixel counts and also retains per-scene metrics. Macro class metrics weight classes equally, unlike overall pixel accuracy. Report unsupported/absent classes explicitly rather than manufacturing a score. None of these measures evaluates legal status or end-to-end causal attribution.

### 10.2 Site evidence awaiting review

| Demonstration evidence | Pending result |
|---|---|
| Live check: area, run ID, provider, actual scene date and terminal state | {{RESULT:live_check_outcome}} |
| Archive replay: case/run IDs, acquisition dates, images and replay badge | {{RESULT:archive_replay_case}} |
| Same-season comparison: reference ID/date/coverage and reasons | {{RESULT:seasonality_comparison}} |
| Authorisation comparison: zone type/date/overlap/source and illustrative flag | {{RESULT:authorisation_comparison}} |
| Department routing: alert ID, channel status, outbox/SMTP evidence | {{RESULT:dispatch_delivery}} |

Screenshots belong under `docs/screenshots/` with acquisition dates, origin, method and IDs recorded in captions. The linked filenames are placeholders. A map tile is not the model's dated input. A saved report is not proof that an officer received it. Record negative outcomes such as no new scene or missing seasonal reference as legitimate evidence.

## 11. Limitations, security and ethics

- The system proposes causes from patterns and optional context. Optical imagery cannot prove the mechanism, actor, intent or legality of change. A burn-scar `NATURAL_EVENT` label does not prove natural ignition.
- Clouds, haze, monsoon gaps, shadows, mixed pixels and registration errors affect results. The spectral gate's median centring can miss AOI-wide changes. Morphology and minimum area can discard small events.
- Seasonality is based on one available reference near last year's date and heuristic thresholds. Recurring water or shifted sowing dates can mislead it; crop-cycle suppression can hide unusual harm.
- OSCD L1C urban imagery differs from live L2A India data. Normalisation reduces but cannot validate away the shift. India forest/land-cover supervision is weak and dated; geographic splits do not guarantee national generalisation.
- An incomplete permit register creates absence-of-record findings. Illustrative approximate zones can influence the code's verdict and must remain visibly qualified. A match is not official legal confirmation.
- A scheduled check does not guarantee a new usable observation. Newest-first selection and finite budgets limit observation coverage. Provider failover does not eliminate all outages; a new replay request needs catalogue access.
- SQLite and in-process queues/events suit a small deployment. Multiple Uvicorn processes require external coordination. Crash recovery is not a substitute for backups, capacity testing or a durable distributed queue.
- Optional password login protects mutating API routes; reads, evidence and model artifacts remain public. It is not departmental identity, roles or tamper-proof audit. TLS, access review, secret management and abuse controls are deployment work.
- `.example` addresses represent seeded routing only. SMTP acceptance is not verified receipt. Avoid naming a person/company as acting illegally; retain review/false-positive outcomes and minimise dissemination of sensitive locations.
- Docker/container execution, final package portability, field validation and external delivery remain unverified by this worker.

## 12. Future scope

Priorities are an independent, geographically varied labelled test campaign; validation of seasonality/cause rules; calibrated uncertainty; authenticated official permission sources with provenance; field-review feedback; and measured sensitivity to clouds, seasons and resolution. Sentinel-1/radar could reduce optical cloud gaps but would require a separate acquisition/model design. Production expansion would use a durable job queue, shared event service, spatial database, object storage, role-based access, observability and controlled retention. These are future work, not current features.

## 13. References and verification notes

Sources checked during this documentation task. No performance statistic is borrowed from these works as a project result.

1. R. C. Daudt, B. Le Saux and A. Boulch, “Fully Convolutional Siamese Networks for Change Detection,” ICIP, 2018. DOI: [10.1109/ICIP.2018.8451652](https://doi.org/10.1109/ICIP.2018.8451652). [Author preprint, arXiv:1810.08462](https://arxiv.org/abs/1810.08462); [author implementation and publication citation](https://github.com/rcdaudt/fully_convolutional_change_detection). Author/title/year/preprint fetched; DOI, venue and pages cross-checked against [publisher-deposited Crossref metadata](https://api.crossref.org/works/10.1109/ICIP.2018.8451652).
2. R. C. Daudt, B. Le Saux, A. Boulch and Y. Gousseau, “Urban Change Detection for Multispectral Earth Observation Using Convolutional Neural Networks,” IGARSS, 2018. [arXiv:1810.08468](https://arxiv.org/abs/1810.08468); [author OSCD dataset page](https://rcdaudt.github.io/oscd/).
3. D. Zanaga et al., “ESA WorldCover 10 m 2021 v200,” 2022, dataset. [Zenodo record](https://zenodo.org/records/7254221), DOI: [10.5281/zenodo.7254221](https://doi.org/10.5281/zenodo.7254221). Verified record identifies title, creators, version and publication date.
4. M. C. Hansen et al., “High-Resolution Global Maps of 21st-Century Forest Cover Change,” Science, 342, 850–853, 2013. DOI: [10.1126/science.1244693](https://doi.org/10.1126/science.1244693). Publisher fetch returned 403; verify via the [University of Maryland author-lab record](https://www.gel.umd.edu/news.php) and [publisher-deposited Crossref metadata](https://api.crossref.org/works/10.1126/science.1244693), which confirms DOI, title, journal, year, volume and pages. Full-text publisher access is not claimed.
5. V. Maus et al., “An update on global mining land use,” Scientific Data, 9, 433, 2022. DOI: [10.1038/s41597-022-01547-4](https://doi.org/10.1038/s41597-022-01547-4). Associated dataset: [Global-scale mining polygons, Version 2](https://doi.pangaea.de/10.1594/PANGAEA.942325), DOI: 10.1594/PANGAEA.942325. PANGAEA verifies creators, year, dataset and related article; publisher fetch was blocked by a redirect.
6. A. Berger, S. Carter, T. Schofield, A. Pickens and J. Reiche, “Looking for the Quickest Signal of Deforestation or Vegetation Disturbance Globally? Turn to GFW's Integrated Disturbance Alerts,” Global Forest Watch, 13 January 2026. [Official article](https://www.globalforestwatch.org/blog/data-and-tools/integrated-deforestation-alerts/) redirects to [Global Nature Watch](https://globalnaturewatch.org/blog/data-and-tools/integrated-deforestation-alerts/). Verified GLAD-L/GLAD-S2/RADD/DIST-ALERT description; this is a service article, not a peer-reviewed SentinelWatch evaluation.
7. Government of India, Ministry of Mines, “Mining Surveillance System,” parliamentary reply released through PIB, 7 August 2024. [Official release, PRID 2042600](https://www.pib.gov.in/PressReleaseIframePage.aspx?PRID=2042600&lang=2&reg=48). Related field-verification description: [PIB PRID 1942813](https://www.pib.gov.in/PressReleaseIframePage.aspx?PRID=1942813&lang=2&reg=48). Official sources describe satellite triggers and State verification; this report makes no claim to use MSS data.
8. Copernicus Data Space Ecosystem, [Sentinel-2 mission description](https://dataspace.copernicus.eu/data-collections/copernicus-sentinel-missions/sentinel-2). Verified mission role and five-day equatorial constellation design; actual usability remains cloud dependent.
9. ESA WorldCover, [Data access and product-version warning](https://esa-worldcover.org/en/data-access). Verified distinction between 2020/v100 and 2021/v200 algorithms.
10. Element 84, [Earth Search official repository](https://github.com/Element84/earth-search). Verified public STAC role and service-availability boundary. SentinelWatch's inspected code uses the v1 endpoint.

Citation verification note: the Crossref API was fetched successfully for [1] and [4]. The author preprints and dataset/official pages support the remaining entries. Fetching a bibliographic record verifies the citation and accessible summary, not every claim in an inaccessible full text.

## 14. Integrator token inventory

Every distinct measurement/result token used in this documentation package is listed below. Replace from registry/evidence only; if unavailable, retain the token and state why. Sample counts and timing belong to measured artifacts, never an estimate.

- `{{METRIC:change_cpu_inference_seconds}}`
- `{{METRIC:change_dataset_split_counts}}`
- `{{METRIC:change_india_forest_test_f1}}`
- `{{METRIC:change_india_forest_test_iou}}`
- `{{METRIC:change_oscd_test_accuracy}}`
- `{{METRIC:change_oscd_test_f1}}`
- `{{METRIC:change_oscd_test_iou}}`
- `{{METRIC:change_oscd_test_precision}}`
- `{{METRIC:change_oscd_test_recall}}`
- `{{METRIC:change_training_seconds}}`
- `{{METRIC:inference_peak_memory}}`
- `{{METRIC:landcover_cpu_inference_seconds}}`
- `{{METRIC:landcover_dataset_split_counts}}`
- `{{METRIC:landcover_india_test_accuracy}}`
- `{{METRIC:landcover_india_test_macro_f1}}`
- `{{METRIC:landcover_india_test_macro_iou}}`
- `{{METRIC:landcover_india_test_mine_f1}}`
- `{{METRIC:landcover_training_seconds}}`
- `{{RESULT:archive_replay_case}}`
- `{{RESULT:authorisation_comparison}}`
- `{{RESULT:dispatch_delivery}}`
- `{{RESULT:live_check_outcome}}`
- `{{RESULT:seasonality_comparison}}`

