# SentinelWatch monitored-site evidence dossier

Research snapshot: 1 November 2026 UTC (local date 2 November). These are **visually verified physical changes and expected demo interpretations**, not measured model performance or legal determinations. No app replay, classifier, alert dispatch or government login was run. Archive output must retain `origin = "archive_replay"`.

## Method and count definitions

Earth Search v1 was queried by POST `/search`, collection `sentinel-2-l2a`, bbox in `[west,south,east,north]`, inclusive UTC date intervals. Public COGs were read with unsigned rasterio window reads, 400 × 400 display arrays, three workers and small GDAL cache; no full Sentinel scenes were downloaded. Bands B04/B03/B02 form each true-colour preview. Fixed display scaling (reflectance / 0.32, gamma 1/1.4) is for viewing, not a quantitative product. Images are north-up in their source UTM grid; image edges approximate the stated geographic bbox. All committed JPEGs were opened and inspected by the researcher.

Radiometry caveat: Earth Search sometimes lists `offset=-0.1` after applying it to the COG. Previews honour `earthsearch:boa_offset_applied=true` and do not apply that offset twice. This follows the provider's [issue #9](https://github.com/Element84/earth-search/issues/9). [Issue #71](https://github.com/Element84/earth-search/issues/71) documents remaining anomalies even when the flag is false. Dark false-flag candidates were rejected in favour of clear same-tile scenes. This dossier does not change application radiometry.

**Replay clear count** means unique acquisition dates on the indicated canonical MGRS tile with catalogue `eo:cloud_cover <= 20%`; duplicate processings and overlapping tiles are excluded. It is a catalogue screening count, **not** a claim that every AOI in the interval was visually checked. **Recent clear count** additionally requires at least 90% AOI-valid SCL pixels on a decimated 120 × 120 grid, excluding SCL 0,1,3,8,9,10. Recent window is **4 August–1 November 2026 inclusive**, the 90 UTC dates available at this snapshot. The recent tile-screen count is also supplied; neither count proves a new change exists. Selected preview AOI validity uses a 400 × 400 SCL read. Haze, smoke and some terrain shadow can escape SCL.

Coordinates were refined from the inspected imagery; they are monitoring AOIs, not surveyed property boundaries. Each AOI is approximately 5–6 km across. All cited public links were fetched; sources unavailable through the web reader were fetched directly to temporary research storage. Source PDFs and scripts stay under `/tmp/sw-sites/`, not in the application repository.

## Verified sites

| Site | Expected verdict | Replay | Replay clear dates | Recent AOI-clear / tile-screened |
|---|---|---|---:|---:|
| Noida International Airport, Jewar | AUTHORISED_ACTIVITY | 2021-03-20 → 2025-03-24 | 153 | 7 / 7 |

### Noida International Airport, Jewar

**Verified visually.** Expected `AUTHORISED_ACTIVITY`; categories: CONSTRUCTION, URBANISATION, OTHER_CHANGE.

- WGS84 bbox: `[77.585, 28.145, 77.635, 28.195]`; canonical tile `43RGM`.
- Replay: **2021-03-20 to 2025-03-24**; 153 distinct tile-screened clear dates.
- Recent 90 days: **7 AOI-clear dates** out of 7 tile-screened dates.

The 2021 patchwork of fields is replaced by a long paved runway in the northern half, large terminal roofs, graded ground and service roads in 2025. Both checked AOIs are clear. This is the strongest construction demonstration.

| Check | Scene (linked STAC record) | UTC acquisition | Tile cloud | AOI valid |
|---|---|---|---:|---:|
| Before | [S2A_43RGM_20210320_1_L2A](https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items/S2A_43RGM_20210320_1_L2A) | 2021-03-20T05:41:02.490000Z | 0.000033% | 100.00% |
| After | [S2B_43RGM_20250324_0_L2A](https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items/S2B_43RGM_20250324_0_L2A) | 2025-03-24T05:41:06.496000Z | 0.000093% | 100.00% |

![Noida International Airport, Jewar, before](preview-jewar-before.jpg)

![Noida International Airport, Jewar, after](preview-jewar-after.jpg)

**Public record.** The fetched clearance letter is dated 9 March 2020, F.No.10-31/2018-IA-III, for Jewar airport Phase I & II. No expiry was independently established here: valid_to is null, meaning unknown, not perpetual approval.

[MoEFCC environmental clearance, 9 March 2020](https://media.niairport.in/-/jssmedia/dxn/portal/documents/environment-clearance-compliance/environment-clearance-approval.pdf).

**Limits.** The pair brackets multi-year construction, not a single-day event. Nearby fields also change with crops. Only CONSTRUCTION inside the illustrative registered platform should match the zone; the document does not establish compliance with every condition.

## Authorisation registry limitations

`app/data/authorised_zones.json` contains only authorisations supported by fetched public documents. Every polygon is **illustrative**: a conservative activity footprint traced from the selected Sentinel-2 preview, not a cadastral or official clearance polygon. Do not use these footprints as legal boundaries, and do not infer authorisation of neighbouring land or a different activity type. All vertices use GeoJSON longitude,latitude order. The per-zone `source` records the tracing date and uncertainties. Null dates mean unknown, not indefinitely valid. Navi Mumbai has separate historical and renewed EC periods.

The registry is deliberately incomplete. “No authorisation on record in this system's registry” describes this dataset, not a failed exhaustive permit search or proof of unlawful activity. The Rawa–Nangal filings explicitly mention nearby Rajasthan leases, but no verified lease polygons were obtained; no invented zone was added. PEKB has documented approval and documented litigation; see its precise case-status correction above. Registry matching must respect activity type and acquisition date.

## Integration decisions and checks still needed

- Use the proposed JSON for review; the research task does not edit `monitored_sites.json` or `seed_sites.py`. Existing sites are matched by name by the seeder; copying a changed bbox/name into the seed file does not update an existing database row. The backend owner must choose a migration/update approach.
- Run real archive replays before claiming classifier/verdict/dispatch success. Expected categories and verdicts here are research guidance, not forced labels. `NEEDS_REVIEW` must remain available when evidence or cause is insufficient; it cannot be guaranteed by choosing a real site. The four requested substantive verdict families are represented.
- The Aravalli pair is weaker than the airports and crosses a jurisdictional boundary. Keep a human review step for assigning any particular excavation to a reported incident or permit. Do not accuse a named company or person.
- Same-season evidence is essential for Kanha. The April 2023 reference was inspected; the backend must actually compare it with April 2024 before demonstrating no-alert behaviour.
- Check Earth Search offset handling in the application separately. This research changed no model, inference or imagery-service code.
