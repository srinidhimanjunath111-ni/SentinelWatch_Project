# SentinelWatch completion checklist

Final documentation package: v3.0.0 on `feature/complete-sentinelwatch`. The existing exam server at port 8000 was used only for read-only GETs, navigation and screenshots. It was not stopped, restarted, checked, replayed or mutated by this task. No application, test, model or training files were edited.

## Status against the brief

| Deliverable | Final status and evidence |
|---|---|
| Replace measurement/result tokens | Complete. Registry-matched measurements and recorded site/proof results replace the tokens; missing detailed records are described honestly. The requested grep returns no matches |
| Correct stale model claims | Complete. Installed change v2, v1 comparison, land-cover results, non-blind selection, out-of-fold uncertainty, city range and WorldCover agreement limits are stated |
| Published comparison | Exact FC-Siam-diff OSCD F1 57.92% confirmed in the fetched arXiv paper, Table 1. Its thirteen-band experiment differs from this six-band implementation; SentinelWatch is below published |
| Site results | Complete. All measured archive pairs, primary category/verdict hectares and grouped-alert counts are in the report. Other Jewar/PEKB/Navi alerts, Rawa uncertainty, Delhi clouds, PEKB contested clearance and Wayanad delayed imagery are explicit. Expected verdicts are hypotheses |
| Current behaviour | Complete. Hard fetch deadline, per-band retry, cached/offline boundaries, grouped alerts, live/replay separation, illustrative-zone review buffer, map-layer policy and short-pair missed-change trade-off are documented |
| Soak and observed failures | Complete. The recorded soak's limits are explicit: ticks occurred, no checks came due; earlier clean-install checks proved scheduler initiation. The observed overnight imagery-read failure interval is not given an unproven cause |
| Demo guide | Walked on the actual UI. Wayanad opens the presentation; saved incident/site links, correct visible labels, group-versus-region area, Jewar authorised selection, recovery and deliberate clean-snapshot reset are documented. No reset or action button was exercised |
| Viva and speaking guide | Reconciled with final measurements and evidence limits; no unfinished-training claim remains |
| Screenshots | All required screens captured at 1920 × 1080, individually below 400 KB, and visually inspected. See [nine captures and provenance](screenshots/README.md) |
| Diagrams | All Mermaid sources rendered successfully using the CLI under `/tmp`. Rendered PNGs are in `docs/diagrams/rendered/`; the report embeds them. Long/dense diagrams retain high-resolution originals for zooming |
| Final project PDF | [Final report](../output/pdf/SentinelWatch_Final_Year_Project_Report.pdf): 38 pages, about 5.9 MB. Title page, contents, page numbers, wrapped/repeated table headers, nine screenshots and ten rendered diagrams. Every page rendered to an image and viewed; no table overflow or text outside margins found |
| Obsolete PDF | Removed with `git rm output/pdf/SentinelWatch_Final_Year_Project_Review_Package.pdf` after building and inspecting its replacement |
| Release notes/index/changelog | Updated for v3.0.0 and final deliverables. SciPy/ReportLab dependency and launcher-presence claims rechecked against code; old missing-dependency claims removed |
| Scope and data | Documentation, screenshots, rendered diagrams and report PDF only. No mutation server was needed on port 8500. Prior runtime proofs were read, not repeated on the demo instance |

## Verification actually performed in this task

Tools were installed only in `/tmp/sentinelwatch-docs-venv` and `/tmp/sentinelwatch-mermaid`. The temporary scripts below are documentation tooling, not application tests. No current full application test-suite or lint pass is asserted.

| Exact command or method | Result and limits |
|---|---|
| `cat models/registry.json reports/ml/*.json training/README.md reports/demo/site_results.json`, followed by separate bounded reads | Read approved measurement sources fully before editing; large combined output required separate reads |
| `cat README.md docs/README.md docs/ENGINEERING_CONTRACT.md` and focused code reads | Rechecked setup, snapshot restoration, provider retry/deadline, classification/seasonal rules, grouping and UI routes/labels |
| `python3 -m venv /tmp/sentinelwatch-docs-venv` | Created isolated documentation tools environment; no project dependency installation |
| `/tmp/sentinelwatch-docs-venv/bin/python -m pip install --quiet playwright pillow markdown pymupdf reportlab` and `... -m pip install --quiet beautifulsoup4` | Completed in the temporary venv |
| `/tmp/sentinelwatch-docs-venv/bin/python -m playwright install chromium` | Browser available for the requested capture workflow |
| `/tmp/sentinelwatch-docs-venv/bin/python /tmp/sentinelwatch-walk.py` | Nine screens captured; zero console errors, zero page errors and zero attempted mutation requests. GET/HEAD-only browser guard |
| `/tmp/sentinelwatch-docs-venv/bin/python /tmp/sentinelwatch-refine.py` | Five follow-up checks passed: Jewar authorised selection, slider arrow-key movement, Wayanad breadcrumb, saved history link and Kerala inbox. Zero browser errors. An initial wrong expected heading timed out, then the corrected real label passed |
| `npm install --prefix /tmp/sentinelwatch-mermaid --no-audit --no-fund @mermaid-js/mermaid-cli` | CLI installed under `/tmp`; no project or system-wide install |
| `/tmp/sentinelwatch-docs-venv/bin/python /tmp/sw-render-diagrams.py` | Final rendering pass: ten diagrams passed, zero failed. The initial CLI width flag was unsupported; corrected to the installed CLI's `--size` option |
| `/tmp/sentinelwatch-mermaid/node_modules/.bin/mmdc -p /tmp/sw-puppeteer.json -i docs/diagrams/dfd-level-0.mmd -o docs/diagrams/rendered/dfd-level-0.png --size 2400 -s 1.5 -b white` | Refreshed layout after visible edge-label crowding; inspected in the final PDF |
| `/tmp/sentinelwatch-mermaid/node_modules/.bin/mmdc -p /tmp/sw-puppeteer.json -i docs/diagrams/dfd-level-2.mmd -o docs/diagrams/rendered/dfd-level-2.png --size 2400 -s 1.5 -b white` | Refreshed earlier-year reference wording; inspected in the final PDF |
| `/tmp/sentinelwatch-docs-venv/bin/python /tmp/sw-pdf-build.py` | Final PDF built from the report Markdown with all nineteen image assets, contents and page numbers |
| `/tmp/sentinelwatch-docs-venv/bin/python /tmp/sw-inspect-pdf.py` and image viewing | 38 pages rendered; all pages viewed, including updated diagram pages. Zero text-boundary violations; 31 outline/contents entries. Visual inspection found no overflowing tables |
| `/tmp/sentinelwatch-docs-venv/bin/python /tmp/sw-check-docs.py` | 47 checks passed, zero errors: local target existence, forbidden/stale states, screenshot dimensions/size, diagram presence, checkpoint/test hashes, measured F1/site values, viva numbering and PDF contents/size |
| Requested recursive token grep over README, release notes, changelog and docs | No output; exit 1 means zero leftover tokens |
| `git diff --check -- README.md RELEASE_NOTES.md CHANGELOG.md docs/ output/pdf/` | Final pass has zero whitespace errors. One trailing-space finding in the first text commit was corrected in the follow-up |
| Fetch `https://arxiv.org/html/1810.08462` | Table 1 confirms the cited published score. The PDF endpoint first timed out; the HTML full text was available |

The prior `offline_proof`, `offline_ui_proof`, `restore_proof` and `soak` results are attributed to `reports/demo/site_results.json`. They were not rerun during this documentation task and are not a new full-suite result.

## Not exercised or not independently verified

- **Docker image never built here (Docker not installed).** The Dockerfile was read; it now includes CPU PyTorch and operational requirements.
- **`scripts/run.ps1` never run on Windows.** Both launcher files are present; no Windows portability pass is claimed.
- **SMTP real delivery not exercised.** Seeded `.example` inboxes are illustrative. Saved-to-outbox is unsent; snapshot export omits message bodies. SMTP acceptance would not prove receipt.
- **Login tested in the test suite only.** No interactive login was exercised here; public reads/evidence remain a deployment limit.
- **Alert chime never heard in headless runs.** No audible verification is claimed.
- **No overnight scheduler run.** Overnight imagery-read observations and the shorter recorded soak do not establish overnight scheduled checking.
- **Verdicts have no field validation.** The local register is incomplete, boundaries are illustrative, and causes/seasonality remain screening heuristics.
- Full provider/run identifiers, detailed seasonal-reference metadata and per-region zone overlap values are absent from the approved site-result artifact. Those sentences were rewritten as evidence limits, not filled with invented values.
- The observed imagery-read failures have no proven cause. Docker, real SMTP delivery, Windows and field validation remain outside the completed evidence package.

## Real UI defect reported, not fixed

For a multi-year pair the backend deliberately seeks a seasonal reference near the baseline period, but the UI's generic reference description/reasons can still say “a year earlier” or “last year.” Jewar's saved case demonstrates this mismatch. The guide tells the presenter to read the actual date. No application code was changed. No JavaScript console errors were observed in the capture or follow-up navigation passes.
