# Real screenshot evidence

Captured on 2 November 2026 from the already-running first-run demo at `http://127.0.0.1:8000`, with source HEAD `e46bfbb` at the start of documentation. Playwright ran from `/tmp/sentinelwatch-docs-venv`; no dependency was installed in the project. Every image is a real 1920 × 1080 browser viewport, JPEG under 400 KB, and was opened for visual inspection. No synthetic imagery or edited screen content was used.

The capture browser allowed GET/HEAD only. It recorded **zero console errors, zero page errors and zero attempted mutation requests**. A follow-up navigation pass verified region selection, keyboard slider movement, Wayanad breadcrumb/history navigation and the Kerala inbox, again without console errors. One initial follow-up locator expected the wrong authorised heading and timed out; it was corrected to the real label, **Construction inside an authorised zone**, and passed. This was a capture-script error, not an application error.

| File | Real route / evidence |
|---|---|
| [operations.jpg](operations.jpg) | `/#/`; restored snapshot: 7 sites, 14 analysed cases, 20 alerts, live/replay separation |
| [wayanad.jpg](wayanad.jpg) | `/#/case/4f26f148-c1f2-422d-901f-419e71688f1b`; Archive replay; 2024-02-05 → 2024-12-16; selected landslide region inside the 76.71 ha group |
| [jewar.jpg](jewar.jpg) | `/#/case/78bb17c0-26d3-4a1e-b2cb-8673f62e2b55`; Archive replay; 2021-03-20 → 2025-03-24; authorised construction region selected, within 633.41 ha group. Other alerts exist in this case |
| [rawa.jpg](rawa.jpg) | `/#/case/ac5baeb8-b7d3-4966-a422-8f763e4cbda6`; Archive replay; 2021-03-05 → 2025-03-24; suspected mining, 3.78 ha group, uncertain attribution |
| [kanha.jpg](kanha.jpg) | Site route; scene timeline and retained findings without alerts. Archive case `5c00605a-f782-4e41-a38b-af2730df9762`, 2023-11-09 → 2024-04-17; recent case `5cac2aa9-db12-4b44-8af2-955a977a5978`. Recorded alert count is zero for each |
| [alerts.jpg](alerts.jpg) | `/#/alerts`; grouped rows and Status / Severity / Category / Department / Origin filters |
| [departments.jpg](departments.jpg) | `/#/departments`, Kerala State Disaster Management Authority selected; saved Wayanad leads and separate email outcome. Seed mailboxes use reserved `.example` addresses |
| [zones.jpg](zones.jpg) | `/#/zones`; real local register, clearly labelled illustrative. No zone was drawn, added or removed |
| [models.jpg](models.jpg) | `/#/models`; installed change v2 and measured evaluation; registry/test provenance in `models/registry.json` and `reports/ml/` |

All captured incident methods are `siamese-unet+spectral` and `landcover-unet`, change version v2, as recorded in `reports/demo/site_results.json`. Installed checkpoint hashes and evaluation splits are in [the registry](../../models/registry.json). UI group totals may use km² and round more than the report; selected-region area differs from group area. Acquisition dates are historical; “ago”, latest catalogued scenes and countdowns are capture-time state.

The map uses imagery without a third-party boundary or label layer by design. It is navigation context, not the dated analysis input or an official legal boundary. Wayanad's after view was delayed by months of cloud; Rawa is the weakest attribution evidence. A suspected verdict means no authorisation on record, not proof of illegality. Verdicts have no field validation.
