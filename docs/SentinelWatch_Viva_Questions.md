# SentinelWatch viva questions

Answers follow the final inspected code, registry and measured reports. The UI was walked read-only. Prior runtime proofs are attributed to `reports/demo/site_results.json`; this task did not rerun analyses or the application test suite.

## Purpose and evidence

1. **What problem does SentinelWatch solve?** It repeatedly screens marked places for physical land change and gives a reviewer dated evidence, reasons and a department review queue.
2. **What is the strongest conclusion it can make?** “Suspected unauthorised, no authorisation on record.” Satellite imagery cannot establish illegality.
3. **What is the difference between category and verdict?** Category proposes a physical pattern, such as mining or flood. Verdict combines seasonal interpretation and permission-record context, such as needs review or authorised activity.
4. **Who is the intended user?** An operator and departmental reviewer. Seeded departments illustrate routing; they do not establish an official government integration.
5. **What is real versus illustrative?** Processing uses real Sentinel-2 observations. Archive replay uses real past dates and is badged. Seeded `.example` recipients and zones marked `is_illustrative` are illustrative; tests may use controlled fixtures that are not presentation evidence.
6. **What is your contribution beyond a change model?** A complete evidence workflow in code: scheduling, quality checks, proposed cause, seasonality, registry comparison, routing, provenance and human outcomes. Its final runtime validation must be reported separately.

## Satellite observations

7. **Why Sentinel-2?** It provides open multispectral observations suitable for vegetation, soil and water comparison, with repeat coverage and visible/NIR detail useful at site scale.
8. **Why not promise real-time detection?** Sentinel-2 revisits roughly every two to five days; clouds and publication delay can extend usable gaps. A check interval polls a catalogue, not the satellite.
9. **Why six bands?** Blue, green and red support surface/visible evidence; NIR supports vegetation/water contrast; two SWIR bands support bare, built and burn-related indices. The contract fixes the same order in data, training and inference.
10. **What is the band order?** `B02, B03, B04, B08, B11, B12`, shaped `[6,H,W]`, float32 reflectance on one grid.
11. **Are all six bands natively 10 m?** No. B11 and B12 are 20 m bands resampled to the 10 m grid. Interpolation does not create new fine detail.
12. **What is L1C versus L2A?** L1C is top-of-atmosphere reflectance; L2A is atmospherically corrected surface reflectance. OSCD training data and live imagery therefore have a domain shift.
13. **Does normalisation remove that domain shift?** It reduces first-order radiometric differences. It cannot prove performance transfers across atmosphere, geography, season or surface types; India forest test F1 is 0.516, but its coarse annual labels and forest-only scoring leave major transfer limits.
14. **Where do images come from?** The code tries Earth Search first, then Planetary Computer on failure, with CDSE as an additional configured fallback. Public-provider access does not require personal CDSE credentials.
15. **What is STAC?** A structured catalogue interface for scene metadata, geometry, dates and asset links. Finding a record does not itself prove the pixels were read successfully.
16. **What happens when a provider fails?** Per-band retry, provider health/cooldown, alternate providers and a hard deadline for remote imagery operations are implemented. If all fail, the run records failure; cached evidence remains historical evidence.
17. **How do you handle clouds?** Read SCL within the AOI, reject no-data/defective/shadow/cloud/cirrus labels, measure local usable coverage, apply a blue/red haze mask when SCL coverage passes, buffer bad edges and compare shared valid pixels.
18. **Why is tile cloud percentage insufficient?** A large tile may be cloudy while the small AOI is clear, or the reverse. AOI-local SCL is closer to the pixels actually analysed.
19. **Why align and reproject?** Corresponding pixels must refer to the same ground area. The common projected UTM grid also makes physical pixel-area calculation meaningful.
20. **How is changed area measured?** Count retained changed pixels and multiply by projected grid pixel area. It is an estimate affected by masks, mixed pixels, thresholding and boundaries.

## Networks and learning

21. **Why a Siamese network?** Shared encoder weights describe both dates consistently. Comparing their features helps predict a dense change mask instead of independently classifying each image.
22. **What is a U-Net?** An encoder-decoder segmentation network with skip features that help recover pixel detail at output resolution.
23. **Is this an exact copy of Daudt et al.?** No. It follows the FC-Siam-diff idea with repository-specific blocks, six-band input and preprocessing. The paper motivates the design; it does not validate this implementation.
24. **Can the change network tell which direction change happened?** Its absolute feature differences are symmetric. Direction is inferred separately from before/after land-cover labels and spectral deltas.
25. **Why a second land-cover network?** A binary change mask says where, not what changed. Single-date classes help propose tree-to-bare, crop-to-built or water transitions.
26. **Which land-cover classes exist?** Tree, shrub/grass, crop, built, bare, water, wetland and mine. Invalid pixels use a separate no-data code.
27. **What happens without a checkpoint?** The run uses explicitly labelled spectral change/land-cover fallbacks. The coarse land-cover fallback cannot directly recognise a mine; do not present it as equivalent to trained inference.
28. **What does model READY mean?** The registry builder found evaluation evidence matching an installed checkpoint hash. That differs from a model merely loading; it also does not mean every application category is validated.
29. **How are large image windows handled?** Inference slides overlapping patches across an allowed AOI and blends outputs. This limits inference memory; the app still enforces a maximum AOI size.
30. **Which normalisation is installed?** v2 uses per-date median/IQR normalisation with alignment and calibration. v1 pooled statistics across dates. Per-date statistics reduce broad radiometric shifts but can suppress genuine change covering most of the AOI. Inference follows the checkpoint, not a guessed default.
31. **What losses do you use?** Change training uses weighted binary cross-entropy plus soft Dice over valid pixels. Land cover uses class-weighted cross-entropy with ignored labels.
32. **How do you address class imbalance?** Weighted losses, positive/rare-class crop sampling and per-class reporting. These controls help optimisation but do not prove balanced real-world performance.
33. **How do you avoid train/test leakage?** OSCD uses city-based separation; India datasets use geographic blocks/tiles and chip margins. v2 threshold/temperature were chosen on out-of-fold validation. However, v1's test score was seen before v2 was designed, and test results helped choose between them. v2 is not fully blind; the selected score can be optimistic. Nearby regions and shared source-label errors remain concerns.
34. **What labels train the models?** OSCD urban change, India L2A pairs supervised by annual Hansen forest loss, and India land cover supervised by WorldCover plus historical mining polygons. Each has stated scope and label uncertainty.
35. **Are WorldCover and mining labels ground truth?** They are reference labels. WorldCover is model-derived; mining polygons describe historical footprints. Test agreement does not establish field truth, legal status or active extraction.

## Metrics and testing

36. **What are precision and recall?** Precision asks how many predicted change pixels were labelled change. Recall asks how much labelled change was found. They trade missed change against false alarms.
37. **What does F1 mean?** It is the harmonic mean of precision and recall: `2TP/(2TP+FP+FN)` for the change class. It measures segmentation agreement on a specified split, not legal correctness.
38. **What is IoU?** Intersection over union: `TP/(TP+FP+FN)`. It compares overlap between predicted and reference change.
39. **Why not use accuracy alone?** Unchanged land can dominate. Predicting no change can look accurate while missing the events of interest; inspect F1, recall, precision, IoU and class-specific results.
40. **What is macro F1?** Compute F1 per class, then average classes equally. Unlike overall pixel accuracy, it gives rare classes equal weight. State how absent classes are handled.
41. **What score did your model achieve?** Installed change v2: OSCD F1 0.461, India forest F1 0.516; v1: 0.248 and 0.101. Land-cover v1: accuracy 0.560, macro F1 0.526, macro IoU 0.373. These are checkpoint-matched reference-agreement scores. Published FC-Siam-diff is 57.92% F1 (about 0.58) with thirteen OSCD bands in [Daudt et al., Table 1](https://arxiv.org/html/1810.08462), so ours is below published. Out-of-fold OSCD F1 is 0.289 and test-city F1 ranges 0.004–0.751; the pooled score has real uncertainty.
42. **Is displayed confidence the model's F1?** No. Finding confidence combines change support, rule strength and seasonal evidence. Priority adds verdict and area. They are review-ranking heuristics, not measured accuracy or probability of wrongdoing.
43. **What tests did this documentation task run?** Read-only browser navigation, screenshot inspection, static documentation/source checks, Mermaid rendering and PDF build/visual inspection, as listed in the checklist. It did not run checks/replays against the demo server, train models or exercise SMTP.
44. **What should the final integrator test?** Real imagery-to-evidence flow, clouds/no-new-scene states, seasonal and registry branches, duplicate merging, delivery outcomes, PDF opening and restart recovery, alongside the current isolated tests.
45. **Why are passing unit tests insufficient?** Controlled fixtures and mocks can verify contracts without exercising provider availability, real registration errors, model generalisation or external delivery.

## Rules, seasonality and permissions

46. **What are NDVI, NBR and NDWI?** Ratios using NIR/red for vegetation, NIR/SWIR2 for burn-related contrast and green/NIR for water. They are evidence signals with confounders, not event or legal labels by themselves.
47. **How do you combine model and spectral evidence?** The code rescales the model around its validation threshold, blends it with spectral support and requires a spectral gate on that branch; sufficiently strong spectral evidence can stand alone. It then cleans and groups pixels. Without a model it uses the spectral method alone.
48. **Can the spectral gate miss a real change?** Yes. The short-pair rule also downgrades proposed human activity when vegetation declines but remains vegetated; it can miss real change on short pairs. Stable-pixel shift estimation and spectral gating still depend on thresholds, and the gate can reject a correct model-only detection. The code preserves widespread change by estimating offsets only from stable pixels and also permits strong spectral evidence alone; these choices still need sensitivity evaluation.
49. **How does the seasonality test work?** Compare the later scene to a usable same-season reference in the direction of the original change. Short pairs use the previous year; multi-year pairs go further back toward the baseline period to avoid a reference already containing the change. The UI's generic “a year earlier” wording can be inaccurate; read the recorded date. Sufficient persistent difference supports non-seasonal change; positive similarity across driving indices supports benign recurrence. A covered region matching neither test remains inconclusive, and missing coverage also yields unknown.
50. **Does one seasonal reference prove harmlessness?** No. Timing, drought, crop shifts and recurring floods can mislead it. Crop cycles are explicitly suppressed by a heuristic; field and multi-year validation are future work.
51. **How do you distinguish flood from ordinary water change?** New water plus rainfall or nearby flood corroboration supports the flood rule. Without it, the code records water change. Missing feeds do not prove no flood.
52. **How do you distinguish construction from urbanisation?** Built-surface change near an already-built surrounding ring is labelled urbanisation by the current rule. Isolated built change is construction. This is a heuristic distinction.
53. **Can you prove mining from a bare patch?** No. The mining rules need mine-class, quarry context or an operator-registered mining site for the relevant soil-change path. The mining-site label is contextual input, not independent confirmation. Even that proposes a footprint/cause, not extraction, ownership or legality.
54. **Does NATURAL_EVENT prove a fire was natural?** No. Burn scars receive that routing verdict, but the imagery does not establish ignition cause. State “possible burn scar” and retain human review.
55. **How does the authorisation test work?** A registered zone must be in force on the scene date, match the category and overlap at least half the region. The first match is used; this is a local-record heuristic, not a legal determination.
56. **What about illustrative zones and OTHER zone type?** Illustrative records participate but carry an explicit qualification; their boundaries are approximate. A 1 km buffer can route nearby unmatched activity to needs review, without extending authorisation. OTHER can match the four human-caused categories, so registry quality and specificity matter.
57. **Why not claim illegality when no zone matches?** The registry may be incomplete, expired, wrongly drawn or missing a lawful clearance. Satellite evidence and absence of a local record are not proof of a legal violation.
58. **Do all findings generate alerts?** No. Seasonal benign findings are retained without alerts. Other findings face evidence and site-category filters; hazards bypass the category filter but not the evidence threshold.
59. **How are duplicates handled?** Eligible regions first group by category and verdict. Compatible active same-category, same-verdict overlapping groups merge and update persistence. Live/replay stay separate and replay merging stays within one replay ID. A repeated new replay can therefore add replay alerts.

## Delivery, scale, security and ethics

60. **How is a department chosen?** Category routes to Forest, Mines & Geology, Planning, Disaster Management or the Pollution Control Board, with state-specific local records. A route is not proof of official jurisdiction.
61. **Does dispatched mean an officer received email?** No. It records workflow dispatch/in-app routing. Check channel outcomes: OUTBOX_SAVED is unsent, SENT is SMTP acceptance, and FAILED is failure. Receipt/reading requires separate evidence.
62. **What is Archive replay?** The same pipeline applied to selected real past scenes, with `origin=archive_replay`. It is visibly distinguished from live monitoring. Starting a new replay still needs catalogue access. The recorded offline proof completed a cached replay, which does not guarantee every new request works offline.
63. **What happens after a restart?** SQLite/evidence remain if their storage is preserved. Scheduler startup requeues unfinished runs in code. The recorded clean-install proof restored snapshot rows and images from an empty database. A deliberate clean reset requires stopping the server, deleting the configured database and its sidecars plus outputs/outbox, then restarting with restoration enabled. Do not reset the exam instance during this read-only task.
64. **Can this scale across India as deployed?** The current bounded AOIs, SQLite and in-process scheduler suit a small project. National scale needs distributed jobs, shared locks/events, spatial indexing, object storage and measured capacity.
65. **What security is implemented?** Optional password login with signed HTTP-only session cookies gates mutating APIs. Reads/evidence remain public. It is not role-based departmental authentication; use TLS and review access before public deployment.
66. **How are secrets handled?** Environment configuration or a local ignored configuration file; never include values in docs, screenshots or commits. This task does not log into department systems.
67. **What are the ethical risks?** False accusations, disclosure of sensitive locations, incomplete records and automation bias. Use suspected wording, restricted dissemination where needed, human verification and recorded false-positive outcomes.
68. **Was Docker tested?** No. Docker is not installed on this machine. The deployment recipe is unverified here; the integrator must check final dependencies, checkpoints, volumes and single-process scheduling.
69. **What are the main remaining evidence gaps?** Docker image never built here (Docker absent); Windows launcher never run on Windows; SMTP real delivery not exercised; login tested in the test suite only; chime never heard in headless runs; no overnight scheduler run; verdicts have no field validation. The recorded soak lasted 45 minutes but no checks came due; earlier clean-install checks prove scheduler initiation, not that soak.
70. **What would you improve next?** Independently validated multi-season evidence, richer temporal checks, calibrated uncertainty, authenticated permit sources, field feedback and durable deployment infrastructure. Radar would need its own acquisition and model work.

If challenged, use: “Here is what the code does, here is the evidence I have, and here is what I have not verified.” Do not substitute an old test count or a literature metric for a project result.
