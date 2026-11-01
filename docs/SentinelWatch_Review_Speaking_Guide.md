# SentinelWatch Review Speaking Guide

## Explain it at three levels

**Beginner level.** Think of SentinelWatch as a watchtower, not a judge. I mark a place on a map. The system looks for satellite observations at two dates and, if it has the required image-processing setup, highlights land that looks different. A human still checks whether it is real and whether it is legal.

**Technical level.** The browser dashboard calls FastAPI endpoints. SQLAlchemy stores AOIs, acquisitions, monitoring runs, analyses, cases, findings, alerts and review actions in SQLite. A CDSE STAC adapter searches metadata. The optional downstream path retrieves raster bands, aligns/masks them, computes spectral/NDVI change, creates connected regions and stores findings. A PyTorch Siamese U-Net is coded but no validated checkpoint is installed.

**Review-panel level.** I chose a simple web/API/database structure so the evidence trail is inspectable and testable. I deliberately show blocked states instead of faking outputs. Code and unit tests verify many modules; the final operational proof still needs credentials, Rasterio, an image pair and a lawful labelled dataset/model evaluation.

## 8–10 minute slide plan

### 1. SentinelWatch: title and idea
- AI-assisted satellite land-change monitoring.
- Decision support, not automatic illegality detection.
- Show: dashboard title/architecture diagram.
- Say: “My project helps a reviewer focus on possible changes. The key word is possible; it creates a lead, not a legal verdict.”
- Transition: “Why do we need such a system?”
- Panel may ask: What is the exact problem? **Answer:** large/remote areas cannot be manually inspected frequently.

### 2. Problem and motivation
- Deforestation, land disturbance and construction can be spatially large and time-dependent.
- Ground verification is still required.
- Show: a simple before/after concept (not a fabricated project result).
- Say: “The goal is to reduce the search area for a human inspector.”
- Transition: “I designed the system around a traceable workflow.”
- Question: Can it prove crime? **No; imagery alone cannot establish legality or permissions.**

### 3. Objectives and scope
- AOI management, official catalogue search, evidence persistence, optional analysis, alerts/reports.
- Explain current boundary: model and live raster path not validated locally.
- Show: completion checklist.
- Say: “I separate code that exists from functionality proven in this environment.”
- Transition: “Here is the actual architecture.”

### 4. As-built architecture
- Browser → FastAPI → SQLite; monitoring service → CDSE; optional Rasterio/model/SMTP.
- Show: `architecture.mmd` rendered in Mermaid viewer.
- Say: “The browser never talks directly to the database or CDSE; the backend controls validation and provenance.”
- Transition: “Now I will break this into modules.”
- Question: Why SQLite? **Simple persistent local database for a final-year deployment; PostgreSQL/PostGIS is a future scale option.**

### 5. Modules and workflow
- AOIs, candidate discovery, metadata search, monitoring, analysis, alerting, reporting.
- Show: DFD Level 1.
- Say: “The candidate module only ranks catalogue availability; it is not a detector.”
- Transition: “The detailed workflow shows important failure branches.”
- Question: What happens if no suitable pair exists? **The run records that state; no alert should be invented.**

### 6. Data flow and UML
- Context, detailed comparison, sequence/ER views.
- Show: DFD Level 2 and ER diagram.
- Say: “The ER diagram shows how a monitoring area leads to runs, analyses, cases, findings and alerts.”
- Transition: “Next are the actual algorithms.”
- Question: Catalogue vs database? **Catalogue is external product metadata; database is the app’s persisted evidence/workflow data.**

### 7. Methodology and algorithms
- STAC search → temporal pair → preprocessing → baseline/model → components → alert.
- Explain NDVI, `NDVI=(NIR-Red)/(NIR+Red)`, only when bands are present.
- Show: activity diagram.
- Say: “Current operational fallback is labelled spectral/NDVI baseline. A Siamese U-Net is code, not a claimed trained result.”
- Transition: “This is how the implementation is organized.”
- Question: Baseline vs ML? **Baseline uses designed rules; ML learns from labelled examples and needs measured validation.**

### 8. Technologies and implementation
- Python/FastAPI, static JS/Leaflet, SQLite/SQLAlchemy, HTTPX/CDSE, NumPy/Pillow; optional Rasterio/PyTorch/ReportLab.
- Show: directory tree and component diagram.
- Say: “I kept the initial front end lightweight and made optional heavy components explicit.”
- Transition: “Now I will show what has genuinely been verified.”
- Question: Why FastAPI? **Typed APIs, automatic docs and concise Python integration.**

### 9. Results and verification
- Dashboard/health opened; 19 tests passed; Ruff passed; real CDSE metadata query returned products.
- No live imagery-to-alert result, no trained-model metric, no SMTP delivery claim.
- Show: dashboard status and testing table.
- Say: “The strongest result is truthful observability: empty/blocked states are displayed rather than hidden.”
- Transition: “These are the remaining gaps.”
- Question: Does a unit test prove production? **No, it proves a controlled behaviour only.**

### 10. Limitations and future work
- Credentials/Rasterio/pair/model/data/evaluation remain needed.
- Clouds, seasonal changes, shadow and spatial misalignment cause false positives.
- Show: checklist P0/P1 tasks.
- Say: “My next review milestone is one reproducible real pair with provenance and human validation.”
- Transition: “I will close with the contribution.”
- Question: How reduce false positives? **Cloud masks, date/pair rules, calibration, field checks and labelled evaluation.**

### 11. Conclusion
- A traceable land-change review platform with an honest operational boundary.
- Human verification remains central.
- Show: one-sentence conclusion.
- Say: “SentinelWatch is useful because it prioritizes review while preserving where each observation and decision came from.”
- Final question: What will you do next? **Configure operational path, collect lawful evidence, validate the baseline/model, and capture real case artifacts.**

## Delivery tips

Never say “the model detects illegal mining” unless you have trained/evaluated it and even then say “potential change.” If asked about an incomplete item, say: “The interface and service contract are implemented and tested with controlled inputs; I have not yet verified the external end-to-end integration, so I would not claim it as complete.” Keep the review focused on evidence, not promises.
