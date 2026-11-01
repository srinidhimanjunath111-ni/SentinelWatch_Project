# SentinelWatch Viva Questions and Answers

## 1. Introduction and problem

1. **What is SentinelWatch?** A web system that helps reviewers find potential land-surface change from satellite observations.
2. **What problem does it solve?** It prioritizes large or remote areas that are difficult to inspect repeatedly on the ground.
3. **Does it prove illegal activity?** No. It produces a lead for human verification, not a legal finding.
4. **Who would use it?** Environmental, forestry, planning, research or compliance reviewers.

## 2. Architecture

5. **What are the main layers?** Browser dashboard, FastAPI backend, service layer, SQLite database and external CDSE services.
6. **Why is there a backend?** It validates input, controls access to services, persists evidence and runs longer monitoring work.
7. **How is long work handled?** A check-now request creates a monitoring run and starts a FastAPI background task; optional scheduling code also exists.
8. **What is external?** CDSE STAC, optional CDSE Process API, map tiles/CDN assets and optional SMTP.

## 3. Modules and data flow

9. **What is an AOI?** Area of Interest: the map geometry to monitor.
10. **What is a monitoring run?** A persistent record of one attempt to search/process a particular AOI.
11. **What is a priority candidate?** A preliminary grid cell ranked by metadata availability; it is not a detected incident.
12. **What is the flow after Check now?** Create run, search metadata, choose a pair, process only if prerequisites exist, then persist analysis/findings/alerts.

## 4. Frontend and backend

13. **Which frontend technology is used?** Server-served HTML, CSS and vanilla JavaScript; Leaflet is used for map functionality when loaded.
14. **Which backend framework is used?** FastAPI running with Uvicorn.
15. **How does the UI communicate?** JavaScript `fetch` calls JSON API endpoints.
16. **What is the API documentation URL?** `/docs` on the running local server.

## 5. APIs and external integrations

17. **What is STAC?** SpatioTemporal Asset Catalog, a standard API pattern for searching geospatial products by place and time.
18. **How does the project find observations?** It posts a search to the configured Copernicus Data Space STAC endpoint.
19. **Catalogue versus application database?** Catalogue is the provider’s external product index; SQLite stores the app’s selected/provenance/workflow records.
20. **What needs credentials?** The source separates public metadata search from authenticated imagery Process API retrieval.

## 6. Database and persistence

21. **Which database is used?** SQLite through SQLAlchemy.
22. **Which important entities exist?** MonitoringArea, SatelliteAcquisition, MonitoringRun, AnalysisRecord, Case, Finding, Alert, Department and NotificationAttempt.
23. **How do you know it persists?** The app uses `instance/sentinelwatch.db`; API/service tests verify persisted records. For a demo, restart and re-query an intentionally created record.
24. **What is a migration limitation?** The project has lightweight additive SQLite migrations, not a fully managed Alembic migration history.

## 7. Satellite imagery and remote sensing

25. **Why is satellite imagery useful?** It gives repeated wide-area observations with an acquisition date and geographic footprint.
26. **What is acquisition date?** The time the satellite measured the scene; it differs from when the software retrieves it.
27. **Why are clouds a problem?** Clouds and shadows can look like surface change or hide the ground.
28. **What is NDVI?** A vegetation indicator: `(NIR - Red)/(NIR + Red)`. It needs red and near-infrared bands and must be interpreted carefully.

## 8. Change detection

29. **How does the baseline compare images?** It normalises illumination, combines RGB/vegetation differences into a probability-like map, thresholds, cleans it and groups connected pixels.
30. **How is changed area calculated?** Number of changed pixels times real pixel area from geospatial metadata.
31. **Why preprocess?** Two images must align and unreliable cloud/shadow pixels must be masked before comparison.
32. **What causes false positives?** Seasonality, crops, clouds, shadows, atmosphere, alignment and sensor differences.

## 9. Machine learning

33. **What is a Siamese network here?** Two images pass through shared encoder weights so their differences can be used to predict change.
34. **What is a U-Net?** An encoder-decoder network that produces a pixel-level output mask.
35. **Is the model trained now?** No validated checkpoint or held-out evaluation is registered; do not claim model accuracy.
36. **Baseline versus model?** The baseline uses designed image rules; the model learns from labelled pairs and must be trained/evaluated.
37. **What metrics would you use?** Precision, recall, F1 and IoU on a held-out labelled set, but only after actual evaluation.

## 10. Alerts and departments

38. **What is a priority score?** A heuristic ranking based on confidence, persistence and area; it is not a probability of crime.
39. **How does an alert reach a department?** An alert can be assigned through the API; a notification attempt is recorded and SMTP is used only if configured.
40. **How are duplicate alerts controlled?** The workflow uses persisted case/finding/alert state and creation logic rather than sending a new alert for every screen refresh.
41. **Can email delivery be claimed?** Not in this local configuration; SMTP was not configured/tested.

## 11. Testing and results

42. **How have you tested it?** `pytest -q` passed 19 tests and Ruff passed; tests cover API, analysis, provider parsing, monitoring states, operations and reporting contracts.
43. **Was real satellite data accessed?** A real CDSE metadata search returned products. Imagery retrieval/analysis was not verified locally.
44. **Why is a unit test not enough?** It uses controlled/mocked dependencies and does not prove credentialed external or real-raster behaviour.

## 12. Limitations, security and future work

45. **How are secrets handled?** Configuration comes from environment variables; documentation must never contain real tokens or passwords.
46. **What are the biggest current limitations?** Missing imagery credentials/Rasterio, no trained model/dataset/metrics, and no live alert/PDF/email evidence.
47. **What is the P0 next step?** Configure lawful CDSE access and run one reproducible AOI through the real pair/preprocessing path.
48. **How would you improve scale?** Use durable workers, PostgreSQL/PostGIS, object storage, user roles and model monitoring.
49. **How do you distinguish demo from live?** `DEMO_MODE` and `demo_seed.py` label synthetic data; normal UI excludes synthetic test fixtures.
50. **What must you verify before saying the project is complete?** Real pair retrieval, preprocessing, measured analysis quality, generated case/alert/report, and any notification claim.

For difficult answers, verify the current dashboard status and the exact test output before the viva; configurations can change.
