# SentinelWatch speaking guide

Use these scripts alongside the [ten-minute demonstration](SentinelWatch_Demo_Guide.md). Their durations are rehearsal targets, not measured application timings. Replace no claim with a guessed result. Check the final registry and actual case method/origin before presenting.

## Thirty seconds

“SentinelWatch monitors places marked on a map using real Sentinel-2 imagery. It checks for new usable observations on a schedule, finds changed regions, and uses land cover, seasonal comparison and a permit register to explain them. Eligible alerts go to the appropriate department inbox. Its strongest verdict is suspected unauthorised, with no authorisation on record. It helps a person investigate; satellite imagery cannot establish illegality.”

## One minute

“Field teams cannot repeatedly inspect every forest, mining belt or growing settlement. SentinelWatch helps them choose where to look. An operator marks a site and selects a catalogue-check interval. The backend gets real Sentinel-2 imagery, masks clouds and compares dates on one grid. A Siamese U-Net can highlight change; a second U-Net identifies land cover. If a model is unavailable, the recorded method says that a spectral fallback ran.

“The system proposes a cause from those signals, checks the same season in an earlier year when a reference exists, and checks a local register of authorised zones. It saves the images, dates, reasons and review history. Eligible findings are routed to a department inbox; email is separate and requires SMTP. Historical runs are clearly badged Archive replay. I would describe a missing permission record as suspected unauthorised, never as proof of illegality.”

## Two minutes

“SentinelWatch is a site-based satellite monitoring and review application. Its problem is practical: physical land change can happen across dispersed places, while field inspection is limited. I want to give a reviewer a traceable lead, with enough evidence to question the result.

“The operator draws a bounded area in India, chooses what to watch for and sets how often to check the catalogue. Sentinel-2 revisits roughly every two to five days; a six-hour check does not create a six-hour image. The provider chain tries public imagery sources and records failures. The pipeline measures clear pixels within the actual area, selects a later scene and older baseline, and seeks a same-season reference from the previous year.

“Six bands are aligned on one grid. A shared-encoder Siamese U-Net supplies change evidence when loadable, and a single-date U-Net estimates land cover. The system combines these with vegetation, water, burn, bare-soil and built-up indices. Rules propose a category, such as tree-cover loss, mining-related change or flood. A labelled spectral fallback keeps the method explicit when learned inference is unavailable.

“Next, the seasonal test asks whether this looks like an earlier year's same season. The authorisation test separately checks date, activity type and overlap against a local zone register. Illustrative zones have approximate boundaries and are not official permits. No matching record means suspected unauthorised, not proven illegal.

“Eligible alerts go to the relevant department inbox, with separate email outcomes. Reviewers can acknowledge, record field verification and close or reject a lead. Archive replay uses real past imagery and stays badged. My evaluation claims must come from the installed checkpoint's held-out reports. Installed v2 scores 0.461 OSCD F1 and 0.516 India forest F1, compared with v1 at 0.248 and 0.101. Land cover scores 0.560 accuracy and 0.526 macro F1. These measure agreement with reference labels, not legality or field truth.”

## Eight-minute report presentation

| Segment | Show | Speaking point |
|---|---|---|
| 0:00–0:45: problem | Operations or report objectives | “The aim is to prioritise field review with repeatable evidence.” State the legal boundary early. |
| 0:45–1:30: observations | Site timeline and dates | “Schedule, acquisition and processing time differ. No new scene is a valid outcome.” |
| 1:30–2:30: architecture | Architecture and DFD 1 | “Browser → API → scheduler/provider/analysis → evidence and department workflow. SQLite remembers the trail.” |
| 2:30–3:30: learned/spectral methods | Models and data; DFD 2 | “The binary model locates change; land-cover and explicit rules propose what changed.” Identify the actual case method. |
| 3:30–4:30: seasonality and permits | Case reasons/reference; Zones | “Recurrence and permission records are separate tests. Missing reference means unknown; missing permit means absence of record.” |
| 4:30–5:30: evidence and dispatch | Incident and inbox | “An alert is a lead. In-app dispatch, saved email and SMTP acceptance are different outcomes.” |
| 5:30–6:30: evaluation | Final registry and matching artifacts | Explain precision, recall, F1 and IoU. State dataset/split/hash. Explain v1 versus v2, the selection caveat, city variation and WorldCover agreement limits. |
| 6:30–7:15: limitations | Report limitations | Discuss cloud gaps, L1C/L2A shift, weak labels, incomplete registry and uncalibrated evidence scores. |
| 7:15–8:00: human workflow and next work | Acknowledge/field verification controls; checklist | “Human outcomes stay in the evidence trail.” Explain field validation and durable deployment as next work. |

## Short answers for difficult moments

**“Is everything complete?”** “The checklist separates inspected code, checks actually run and pending integration evidence. I will only call a flow verified if I have its run and artifact.”

**“Where is your accuracy?”** “It belongs to a specific checkpoint and held-out split. Installed change v2 has OSCD F1 0.461 and India forest F1 0.516. Land cover has accuracy 0.560 and macro F1 0.526 against reference labels. Published FC-Siam-diff is about 0.58 on OSCD, above ours. v2 is not fully blind; out-of-fold OSCD F1 is 0.289 and city F1 ranges 0.004–0.751, so there is real uncertainty.”

**“Is that illegal mining?”** “It is a physical pattern consistent with mining-related change. A local permission record may be absent, but only competent verification can establish the legal position.”

**“Is that authorised?”** “The code found a matching local registered zone. If it is illustrative, I can only demonstrate the test, not claim an issued clearance.”

**“Why is the screen waiting?”** “A check is waiting for or processing actual observations. Satellite revisit, cloud cover and network access constrain it. I can show completed real historical evidence with its Archive replay label.”

**“Did that email reach government?”** “The seeded address is reserved `.example`. I can show the inbox record and channel status. I cannot claim government receipt.”

**“Did you inspect this on Docker?”** “No. Docker is not installed on this machine. Deployment remains unverified here.”

**“Is a fire necessarily natural?”** “No. The application's NATURAL_EVENT label routes a burn-scar lead. It does not identify ignition cause.”

## Presentation discipline

Read dates, method labels and origin badges aloud before interpreting an image. Treat an unavailable reference/feed as missing evidence. Explain confidence as a ranking heuristic. Describe test fixtures only as tests. Keep the PDF distinction clear: a case investigation PDF is an app artifact; the final project PDF now contains the measured results, real screenshots and rendered diagrams. The obsolete review package is removed.


## Evidence opening and limits

“Start with Wayanad: the dated comparison shows a 76.71 ha landslide-pattern group, but the after image is delayed by months of cloud, not immediate detection. Jewar's 633.41 ha construction group matches an illustrative zone; other alerts remain separate. Rawa's 3.78 ha mining group is the weakest evidence, with uncertain attribution. Kanha retains 1,912.63 ha of seasonal vegetation without alerts. Delhi's 152.93 ha flood result is cloud-limited. PEKB's clearance is contested.”

“The recorded soak ran for 45 minutes with no overview HTTP failures or logged errors. The scheduler ticked but no sites became due; earlier clean-install checks proved scheduler initiation. There was no overnight scheduler run. A roughly 75-minute imagery-read failure window was observed during overnight testing; the cause is unproven. Hard fetch deadlines and per-band retry now limit waiting. The short-pair rule can miss real change, and no verdict has field validation.”
