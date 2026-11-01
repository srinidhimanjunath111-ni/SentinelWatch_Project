# SentinelWatch v3.0.0 release notes

Documentation prepared 2 November 2026 on `feature/complete-sentinelwatch`. This is a source-based integration handoff, not certification of the final package. Other workers are still landing backend, UI and ML work. Measured model/site results remain pending; the project report contains the exact tokens.

## Behaviour

- Mark bounded places in India, schedule catalogue checks, pause/resume and queue a manual check.
- Use real Sentinel-2 L2A imagery through Earth Search and Planetary Computer, with CDSE as an optional configured provider. Record failures and cache cropped imagery windows.
- Preserve run progress, scenes and quality, dated image evidence, case findings, plain-language reasons and review history. SSE supplies updates with frontend refresh/reconnection support.
- Apply Siamese change inference and single-date land-cover inference when their checkpoints load; explicitly label spectral fallbacks. Training/evaluation tooling and checkpoint-matched model registry are present; a commit saying “trained” does not authorise quoting unreviewed performance.
- Propose change categories using spectral/land-cover/context rules, compare with a prior-year seasonal reference, and separately check local authorisation records.
- Route eligible alerts to local department inboxes and retain channel outcomes. SMTP is used only when configured; otherwise the unsent message is saved with `OUTBOX_SAVED`. Authorised activity is an in-app low-priority notice when eligible.
- Replay selected real past scenes with an **Archive replay** badge, kept distinct from live monitoring. No synthetic-data presentation mode is documented.
- Provide incident comparison, timeline, context, departmental inboxes, zone management, model facts and case investigation PDF routes.

## Existing-installation notes

Back up SQLite and the associated storage together before integration/deployment. Startup performs lightweight additive schema handling. Keep evidence provenance and origins intact; no destructive migration procedure is prescribed here. Preserve model checkpoints, `models/registry.json` and matching `reports/ml/` artifacts. Restart after replacing checkpoints because inference instances are cached.

The inspected operational requirements omit SciPy although analysis imports it; the README manual commands include SciPy explicitly. Reconcile that dependency before claiming a clean installation or container startup. Use the final launch scripts once verified, or the manual virtual-environment setup in the README. Do not rely on the previous credentials-blocked or synthetic demonstration instructions. A public-provider run does not require CDSE credentials. Existing metadata-only and RGB dataset helpers are not the current six-band training recipe.

The inspected deployment uses one application process with an in-process scheduler/event bus. Do not scale by adding Uvicorn processes without coordination. The inspected Docker image recipe needs final reconciliation with the ML dependency/checkpoint requirements; **Docker is not installed on this machine and deployment is unverified here**.

## Evidence and limitations

Sentinel-2 revisits roughly every two to five days; scheduled catalogue polling does not guarantee a new usable image. Clouds, haze, weak reference labels, domain shift, temporal gaps and heuristics constrain interpretation. The local authorisation register is incomplete; illustrative zones are approximate and participate only as explicitly qualified local records. The strongest verdict remains **suspected unauthorised, no authorisation on record**. A satellite cannot establish illegality or identify a responsible actor.

Seeded mailboxes use reserved `.example` addresses. `dispatched` records workflow routing, `SENT` records SMTP acceptance, and neither proves officer receipt. Optional password login protects mutating APIs; reads/evidence remain public.

The documentation task ran static checks only, as recorded in the completion checklist. Final model metrics, inspected site results, screenshot capture, browser rehearsal, full current test/lint results, launch portability, real delivery and deployment remain **pending integrator check**. No new project PDF was built; the user assigned obsolete PDF deletion to the integrator.

## History basis

Changes above are grounded in the repository log: the operational console (`414d95f`), offline/reconnection UI work (`4926f88`), v3 backend (`5363b41`), API/UI alignment (`21a175a`), report/error/timeline refinements (`6a121e2`, `c22a195`), ML contract/training tooling (`2e92945`) and dataset/chart display (`65a9fd7`). Site research commits are listed separately in the changelog; their titles do not substitute for end-to-end case evidence.
