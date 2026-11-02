# Changelog

Source: `git log --oneline -30` on `feature/complete-sentinelwatch`, re-read while preparing this documentation. Entries describe committed work; they do not imply runtime verification by the documentation worker. Work is not assigned a fabricated commit ID or test result.

## v3.0.0 — final demonstration package, 2 November 2026

### Final measured package

- Installed v2: OSCD F1 0.461 and India forest F1 0.516; v1 comparison 0.248 and 0.101. Land-cover accuracy 0.560 and macro F1 0.526. Registry/report reconciliation includes non-blind selection, 0.289 out-of-fold OSCD F1, city variation and reference-label limits.
- `c290d23` — v2 replacement after the v1 investigation; `190ce3a` — training guide, dataset summaries and CPU timing.
- `2107429` — bounded imagery fetches and cached replay during outages; `c4dc5c6` — illustrative-boundary review routing.
- `0621e70` — real v2 first-run snapshot and measured site results; `8f6d585` — fresh-clone restoration, uncached timing and offline recovery proof; `d53420e` — offline browser comparison and PDF proof; `e46bfbb` — measured soak results. The 45-minute soak ticked but ran no due checks; scheduler-started checks were proved by the earlier clean-install run.
- Final docs reconcile grouped alerts, live/replay separation, per-band retry, hard fetch deadline, 1 km illustrative-zone review buffer, map-layer policy and short-pair trade-offs. Observed overnight imagery-read failures have no proven cause; no overnight scheduler run is claimed.
- Real UI screenshots, read-only demo rehearsal, final project report PDF and rendered Mermaid diagrams replace placeholders and the obsolete review package. Known Docker, Windows, SMTP, login, chime and field-validation limits remain explicit.


### Monitoring, evidence and workflow

- `5363b41` — `feat: v3 API, provider chain, pipeline, scheduler, SSE and dispatch`: public-provider chain, persisted runs/evidence, scheduled processing, replay and department delivery workflow.
- `907f5c3` — `docs: add engineering contract for v3 workstreams`: shared vocabulary, band/model/API contracts and ownership rules.

### Operational console

- `414d95f` — `feat(ui): replace dashboard with operational console`.
- `4926f88` — `fix(ui): detect offline base map, recover stream from failed polls, tidy forms and dispatch text`.
- `21a175a` — `feat(ui): align with real API payloads`.
- `6a121e2` — `fix(ui): explain report failures, clean server codes from sentences, compact timeline`.
- `65a9fd7` — `fix(ui): group model datasets per dataset, validated chart colour`.
- `c22a195` — `fix(ui): badge replay scenes on the timeline, dark thin scrollbars`.

### ML and reproducibility

- `2e92945` — `feat(ml): train change model v1 on OSCD and add contract inference`: model/training/inference work committed by the ML stream. This changelog does not quote metrics or claim the final installed model is independently verified.

### Site research and provenance

These are research-stream commits, not automatic claims that an application alert or a legal determination was verified by this worker.

- `7ec42c1` — Jewar construction imagery and clearance research.
- `53b447b` — Navi Mumbai construction and environmental-clearance period research.
- `8f2c933` — PEKB pit-change research and contested-clearance record context.
- `6ef9e32` — Border quarry-change research with registry limitations.
- `09458d1` — Wayanad debris-flow research and corridor identity correction.
- `aed82b6` — Kanha seasonal-cycle research using same-season imagery.
- `54bf4c1` — Cloud-limited Delhi flood replay and evidence-audit documentation.

### Documentation

- `2e8c299` — `docs: replace stale review with v3 report and setup guide`: new report and README; removed obsolete Markdown project review.
- `ae46411` — `docs: align ten system diagrams with v3 implementation`: architecture, DFD, workflow, component, deployment and ER sources.
- `721042e` — `docs: add honest v3 demo script and viva preparation`: ten-minute script, recovery plans, speaking scripts and 70 viva answers.
- The final documentation handoff also reconciles current rule/preprocessing changes and delivers the checklist, index, screenshot instructions, release notes and changelog. Its own commit is intentionally not self-referenced by a made-up hash.
- Measured model/site values now replace all result placeholders. Docker deployment remains unverified. The final project PDF replaces the removed obsolete review package.

## Initial repository

- `c53d999` — `Initial SentinelWatch final-year project`: initial application and older review material. Its earlier status descriptions and demonstration instructions are superseded by this v3 documentation; old test counts are not carried forward as current evidence.
