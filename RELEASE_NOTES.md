# SentinelWatch v3.0.0 release notes

The final documentation package on `main` reconciles installed models, measured site results and the current UI. The final report PDF includes genuine screenshots and rendered diagrams. Runtime proofs below come from `reports/demo/site_results.json`; the documentation walk used read-only navigation on the existing demo.

## Measured results

| Measure | v1 | Installed v2 |
|---|---:|---:|
| OSCD test F1 | 0.248 | 0.461 |
| India forest test F1 | 0.101 | 0.516 |

Land-cover v1: **0.560 accuracy**, **0.526 macro F1**, **0.373 macro IoU**, measuring agreement with WorldCover and historical mining polygons rather than ground truth. The registry and checkpoint-matched test reports are authoritative.

v2 is **not fully blind**: v1's test result was seen before v2 design and test scores helped select the winner. Out-of-fold OSCD F1 is **0.289**; test-city F1 ranges **0.004–0.751**. The pooled 0.461 carries real uncertainty. Published FC-Siam-diff reports **57.92% F1 (about 0.58)** with thirteen bands in [Daudt et al., Table 1](https://arxiv.org/html/1810.08462); this six-band implementation is below published, with different preprocessing.

The first-run snapshot records **7 sites, 14 cases and 20 grouped alerts (3 live, 17 replay)**. [Full site table](docs/SentinelWatch_Project_Report.md#102-measured-site-results): Jewar authorised construction 633.41 ha, Navi authorised construction 133.51 ha, PEKB registered-zone mining 53.75 ha, Rawa suspected mining 3.78 ha, Wayanad landslide 76.71 ha, Kanha seasonal vegetation 1,912.63 ha with zero alerts, Delhi flood 152.93 ha. Hectares are the stated primary category/verdict only. Jewar, PEKB and Navi have other alerts too.

Rawa is the weakest evidence with uncertain tile/incident attribution. Delhi is cloud-limited. PEKB's clearance is contested. Wayanad's after image follows months of cloud; it does not prove immediate detection. Research “expected” verdicts were hypotheses. No verdict has field validation.

## Current behaviour

- Public Sentinel-2 provider chain, per-band retry and a hard remote-imagery deadline. The production fetch budget is 120 s per operation, not an end-to-end guarantee.
- A roughly 75-minute imagery-read failure window was observed during overnight testing (03:34–04:48 IST). Its cause is unproven; it motivated the fetch deadline.
- Grouped alerts by category/verdict with individual evidence beneath them. Live and replay stay separate; replay merging stays within one replay ID. Repeating a replay can add replay alerts.
- Short-pair vegetation evidence rule reduces browning false alarms and can miss real change on short pairs.
- Illustrative zones have a 1 km review buffer for boundary uncertainty, without extending authorisation. The register is incomplete; the strongest verdict is **suspected, no authorisation on record**. Imagery cannot establish illegality or identify a responsible actor.
- Map imagery has no third-party boundary or label layer by design. Local site/zone geometry is not an official boundary service.
- Offline proof: cached replay completed, evidence/PDF remained available, unreachable-provider failure was bounded under a shorter test budget and the same process recovered. Offline browser proof loaded all comparison layers, moved the slider and downloaded a valid case PDF with external requests blocked. New catalogue queries and uncached imagery still need network.
- Recorded soak: 45 minutes, 46 samples, maximum RSS 461 MB, maximum overview response 0.055 s, zero non-200 overview responses and zero logged errors. The scheduler ticked but no checks came due. Scheduler-started checks were proved by an earlier clean-install run, not this soak.

## Setup and restoration

Operational requirements now include SciPy and ReportLab. Both launch scripts are present and attempt PyTorch installation. The Dockerfile installs CPU PyTorch and operational requirements and copies model/report/snapshot assets; execution remains unverified.

Preserve the SQLite database and storage together. Snapshot restoration runs only on an empty database with restoration enabled and the shipped snapshot available. It copies historical rows/JPEG evidence, not raw windows or outbox bodies. For a deliberate clean rehearsal reset, stop the server, delete the configured `instance/sentinelwatch.db*` plus `storage/outputs` and `storage/outbox`, then restart; see the [demo guide](docs/SentinelWatch_Demo_Guide.md). This reset was not executed on the exam instance. Use one application process for the in-process scheduler/events.

## Known limitations

- Docker image **never built here**: Docker is not installed.
- `scripts/run.ps1` **never run on Windows**.
- SMTP real delivery **not exercised**. Seed addresses use `.example`; saved-to-outbox is unsent and SMTP acceptance would not prove receipt. Snapshot history records zero emails sent.
- Login **tested in the test suite only**; no interactive login during this documentation walk. Reads/evidence remain public; this is not departmental role-based authentication.
- Alert chime **never heard in headless runs**.
- **No overnight scheduler run**; the shorter soak cannot substantiate that claim.
- Verdicts have **no field validation**. Clouds, weak reference labels, domain shift, temporal gaps, approximate zones and heuristic cause/seasonality rules constrain results.
- The UI's generic “a year earlier” reference wording can be inaccurate for multi-year pairs: Jewar's saved reference is from the baseline period. Read the actual reference date.

No new analyses or data mutations were triggered on the running demo. The documentation task does not assert a new full application-suite or lint pass; exact documentation/browser/PDF checks are in the [completion checklist](docs/SentinelWatch_Completion_Checklist.md).
