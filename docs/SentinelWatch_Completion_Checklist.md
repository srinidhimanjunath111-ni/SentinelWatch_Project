# SentinelWatch completion checklist

Documentation handoff: 2 November 2026, branch `feature/complete-sentinelwatch`. Source is changing in a shared checkout. **Source inspected** means the implementation was read; it is not a runtime pass. **Pending integrator check** means this worker did not verify the final behaviour. Training/model measurements remain pending even when a training commit exists.

## Deliverables

| Brief item | Evidence and status |
|---|---|
| Root README | Rewritten: product/boundaries, placeholder screenshot links, macOS/Linux and Windows commands, configuration, architecture, sources, actual training CLI, deployment and limits |
| Final-year report | `SentinelWatch_Project_Report.md` delivered with abstract, problem/objectives, verified literature, modules/design, rule order, seasonal/authorisation tests, database, testing, result tokens, limitations and references |
| Replace stale Markdown review | `SentinelWatch_Project_Review.md` deleted after the replacement report was written |
| Ten Mermaid diagrams | All ten rewritten; parser passed 10, failed 0 after fixing a sequence-label syntax error |
| ER accuracy | Checked against AST of `app/models.py`: 16 entities, 122 selected actual columns and all 23 declared foreign-key columns represented; optional one-to-one ORM intent explicitly distinguished from missing unique constraints |
| Use-case/API alignment | Source read: routers, schemas, lifecycle table and frontend hash routes; browser/API interaction remains **pending integrator check** |
| Ten-minute demo | Timed script with actual visible controls, conditional completed-replay path and Wi-Fi/provider/scene/projector/restart/model/email/PDF recovery |
| Viva bank | 70 numbered questions with concise answers, including metrics, domain shift, false positives, permission limits, scaling/security/ethics and real versus illustrative evidence |
| Speaking guide | Thirty-second, one-minute, two-minute and longer report scripts; no invented software timings |
| Release notes/changelog | v3.0.0 notes tied to actual git-log commits, with source/runtime distinction; re-read the log before final integration |
| Documentation index | Updated navigation; old review links removed from owned documents |
| Screenshots | Six explicitly declared placeholder links and rewritten capture instructions; capture/visual review **pending integrator check** |
| Obsolete PDF | New PDF requirement documented. User explicitly directed this worker to leave obsolete `output/pdf/SentinelWatch_Final_Year_Project_Review_Package.pdf` deletion to integrator. No PDF built here |
| Scope | Only README, release/changelog and owned docs modified; engineering contract and `docs/sites/` untouched by this worker |

## Product readiness against the brief

| Requirement | Inspected evidence | Status |
|---|---|---|
| Mark Indian sites, schedule/pause/check | Area API/schema, add-site/site views and scheduler | Source inspected; end-to-end **pending integrator check** |
| Public real Sentinel-2 imagery | Earth Search/Planetary Computer provider chain, cropped reads/cache and optional CDSE | Source inspected; live pair and failover **pending integrator check** |
| Six-band alignment, cloud/quality handling | Provider/grid/SCL and monitoring code | Source inspected; representative raster inspection **pending integrator check** |
| Learned change/land-cover inference | Model interfaces, networks, preprocessing, training/evaluation/registry scripts | Source inspected; final installed checkpoint/evaluation **pending integrator check** |
| Proposed cause/rule table | Ordered rules and vocabulary read; report describes constants as settings | Source inspected; category-level field validation **pending integrator check** |
| Seasonality | Prior-year reference selection and persistence/similarity logic read | Source inspected; final source/evidence branch checks **pending integrator check** |
| Authorisation | Acquisition-date/type/overlap zone filtering; illustrative qualification read | Source inspected; authentic register coverage and case outcomes **pending integrator check** |
| Department routing/deduplication | Category routes, alert eligibility/overlap merge and dispatch code | Source inspected; final flow **pending integrator check** |
| Delivery truthfulness | In-app RECORDED, unsent OUTBOX_SAVED, SMTP SENT/FAILED outcomes | Source inspected; external delivery **pending integrator check**; no department login performed |
| Case evidence and PDF | Persistence/payload/evidence/reporting paths and UI error handling read | Source inspected; actual PDF generation/opening **pending integrator check** |
| Live/replay separation | Origin/replay IDs, replay API, badges and duplicate-isolation code | Source inspected; final browser rehearsal **pending integrator check** |
| Startup/recovery | Scheduler orphan recovery, per-AOI queue and storage code read | Source inspected; actual restart exercise **pending integrator check** |
| Launch scripts | `scripts/run.sh` and `scripts/run.ps1` absent at initial inspection | Presence/final contents and Windows/macOS/Linux execution **pending integrator check**; README includes manual fallback |
| Fresh-install dependency completeness | Analysis imports `scipy.ndimage`; inspected operational requirements omit SciPy. README manual setup explicitly includes it | Backend requirement/script/image correction and clean setup **pending integrator check** |
| Deployment | Dockerfile/Compose/config/auth inspected | Docker not installed; **unverified here, pending integrator check** |
| Security/ethics | Optional write login, public reads/evidence, bounded inputs, suspected wording and illustrative records | Source inspected; deployment threat review **pending integrator check** |
| Current tests/lint | Existing test source read; no suite/lint invocation by this worker | Final suite results **pending integrator check**; do not reuse historical pass counts |

## Checks actually run by this worker

| Exact command / method | Result and limits |
|---|---|
| `git log --oneline -30` | Read repeatedly as work landed; release/changelog describe real commits, not invented history |
| `git diff --check -- README.md docs/SentinelWatch_Project_Report.md docs/SentinelWatch_Project_Review.md` | Passed before the first documentation commit; 0 whitespace errors |
| `npm view mermaid@10.9.5 dist.unpackedSize dependencies --json` | Inspected parser footprint before installing; no browser/Chromium download |
| `npm install --prefix /tmp/sentinelwatch-doc-parser --ignore-scripts --no-audit --no-fund mermaid@10.9.5 jsdom@24` | Completed under `/tmp`; no project dependency/config changes or system-wide installation |
| `NODE_OPTIONS=--max-old-space-size=192 node /tmp/sentinelwatch-doc-parser/check.mjs` | Final parse: **10 passed, 0 failed**. Initial harness setup needed a Node navigator-property fix; initial diagram pass found 1 sequence-label error, corrected and rechecked. Syntax parsing is not rendered-layout review |
| `python3 /tmp/sentinelwatch-doc-parser/check_docs.py` | Static relative-link, stale-copy, token inventory, question count and ER-column/FK check; final output recorded below. Initial run detected the stale checklist assertion, which was removed |
| `python3` inline `ast.parse` of analysis, monitoring, scheduler and config source | All 4 parsed; no duplicate top-level definitions at inspection. Static syntax only; no imports/runtime verification |
| `python3` inline `urllib.request` fetch of `https://api.crossref.org/works/10.1109/ICIP.2018.8451652` and `https://api.crossref.org/works/10.1126/science.1244693` | Both requests succeeded and confirmed DOI/title/venue/year metadata. No training library imported |
| Web search/open of author, dataset and official-service references | Verified the bibliography's required sources; Science publisher returned 403 and Nature redirected, so Crossref/author/PANGAEA records are cited honestly |

Final static-check output: **10 Markdown files; 48 relative links; 6 explicitly declared missing screenshot placeholders; 0 errors. ER: 16 entities, 122 selected columns, 23 actual foreign-key columns. Viva: 70 questions. Tokens: 23 distinct tokens inventoried (18 metric, 5 result).** Final `git diff --check -- <explicit owned paths>` also passed with 0 whitespace errors. The temporary checker scripts contain only static file/AST processing and Mermaid parsing. They do not launch the app or load a checkpoint.

## Integrator actions before presentation/submission

- Reconcile documentation with the final backend/UI/ML commits, especially changes still being edited at handoff. Read the current rule implementation before presenting it.
- Fill every measurement/result token in the report inventory from checkpoint-matched registry/reports and inspected actual run/case evidence. If absent, leave it pending rather than substituting an estimate.
- Reconcile the missing explicit SciPy operational dependency. Verify launch scripts or use the documented manual setup. Run the final appropriate isolated test/lint commands once; report their actual counts separately from these static documentation checks.
- Rehearse the ten-minute click path on the presentation machine and projector. Inspect one completed real case with dates, origin, method, seasonal reference state, zone qualification and channel outcomes. Do not depend on a new live scene.
- Capture genuine screenshots using the six documented names and provenance. Check that no sensitive information is visible.
- If email is demonstrated, use an explicitly permitted controlled recipient and distinguish SMTP acceptance from receipt. `.example` seeds cannot receive government mail.
- Delete the obsolete project-review PDF and build a new report PDF only after reconciling final sources/tokens. This worker intentionally does neither.
- Verify deployment independently; this machine has no Docker. Review public evidence/read access and single-process scheduler assumptions before sharing a public URL.
