# SentinelWatch Completion Checklist

| Requirement | Current status | Evidence | Priority | Next action |
|---|---|---|---|---|
| App startup/health | Complete | local `/api/health` returned 200 | P0 | repeat before review |
| Frontend/backend connection | Complete | dashboard loaded live API state | P0 | retain screenshot after final setup |
| Persistent database | Complete for local SQLite | status says connected; tests pass | P0 | restart test with a test record |
| AOI CRUD/run API | Complete/tested | routes and tests | P0 | use a real small AOI |
| Automatic candidate discovery | Complete/tested with controlled provider | service/API tests | P1 | perform/capture real catalogue discovery |
| Real catalogue search | Complete for metadata | live CDSE query returned 10 products | P0 | record final AOI query |
| Imagery retrieval | Not verified | needs OAuth credentials | P0 | configure CDSE Process API |
| Raster preprocessing | Implemented, not verified locally | `geospatial.py`; Rasterio absent | P0 | install/test Rasterio |
| Baseline change algorithm | Complete/tested in controlled arrays | analysis tests | P1 | validate against real labelled pair |
| Siamese model | Partial | model/training code; NOT_TRAINED | P1 | dataset, train, checkpoint, metrics |
| Map visualization | Partial | Leaflet UI code | P1 | capture a real AOI/map |
| Case/alert generation | Complete/tested contract | services/API tests | P1 | obtain real result |
| Department routing | Complete/tested contract | operations API tests | P1 | assign real alert |
| Notification delivery | Implemented, unverified | notification status code; SMTP absent | P2 | configure/test SMTP |
| PDF report | Implemented, unverified | report route/service | P1 | generate/open genuine PDF |
| Scheduler/background robustness | Partial | background task; scheduler disabled | P2 | enable/verify durable execution |
| Automated tests | Complete | 19 passed | P0 | rerun after changes |
| Lint | Complete | Ruff passed | P0 | rerun after changes |
| Real screenshots | Incomplete | live dashboard visually inspected; no export artifact saved | P1 | capture dashboard/AOI/result/alert/report after P0 path works |
| Review documentation | Complete | `docs/` package | P0 | rehearse from speaking guide |
| Presentation/viva prep | Complete | speaking guide/question bank | P0 | practise honest answers |

Status meanings: **Complete** has direct code plus test/execution evidence; **Partial** has incomplete or unverified important behaviour; **Not verified** is deliberately not claimed complete.
