# SentinelWatch model training

Everything here runs from the repository root with `.venv/bin/python`. Downloads go to `data/raw/`
and caches to `data/processed/`, both gitignored. Measured reports and figures are committed under
`reports/ml/`, and `models/registry.json` is generated from them. Nothing in the registry is typed
by hand.

## Setup

```sh
uv pip install --python .venv/bin/python -r requirements-ml.txt
```

Training used an Apple M1 with 8 GB of RAM on MPS, with `PYTORCH_ENABLE_MPS_FALLBACK=1`. The scripts
set that and an MPS memory cap themselves. Inference also runs on CPU, which is what Docker uses.
Set `SENTINELWATCH_ML_DEVICE=cpu` to force it.

## Reproduce

Test evaluation refuses to run twice on the same checkpoint and dataset unless given `--force`. Run
the test steps once, at the end.

### 1. Change model v1 (OSCD only)

```sh
.venv/bin/python -m data_tools.fetch_oscd
.venv/bin/python -m training.train --datasets oscd --version v1 --run-dir data/processed/runs/change_v1 \
    --steps 4000 --val-every 200 --base-channels 32 --batch-size 8
cp data/processed/runs/change_v1/best.pt models/checkpoints/change_siamese.pt
.venv/bin/python -m training.evaluate export-change --run-dir data/processed/runs/change_v1 \
    --report reports/ml/change_v1_training.json
.venv/bin/python -m training.evaluate change --checkpoint models/checkpoints/change_siamese.pt \
    --dataset oscd --report reports/ml/change_v1_oscd_test.json
.venv/bin/python -m training.diagnose_change --run-dir data/processed/runs/change_v1 \
    --report reports/ml/change_v1_investigation.json
```

v1 was trained before the sampler's positive-crop enrichment, misregistration augmentation and
batch-size rule changed, so re-running this command today gives a similar but not identical model.
Commit `2e92945` holds the exact v1 training code.

### 2. Land cover

```sh
curl -sSfL -o data/raw/naturalearth/ne_50m_admin_1.geojson \
    https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_1_states_provinces.geojson
curl -sSfL -o data/raw/mining_maus2022/global_mining_polygons_v2.gpkg \
    https://download.pangaea.de/dataset/942325/files/global_mining_polygons_v2.gpkg
.venv/bin/python -m data_tools.build_india_landcover --max-minutes 35
.venv/bin/python -m training.train_landcover --version v1 --steps 5000 --val-every 250 \
    --run-dir data/processed/runs/landcover_v1 --report reports/ml/landcover_v1_training.json
cp data/processed/runs/landcover_v1/best.pt models/checkpoints/landcover_unet.pt
.venv/bin/python -m training.evaluate landcover --checkpoint models/checkpoints/landcover_unet.pt \
    --report reports/ml/landcover_v1_test.json
```

The two parent directories above must exist first, and each holds one untrusted download.

### 3. Change model v2 (OSCD plus India forest)

Download the Hansen tiles `lossyear` and `treecover2000` for 30N_070E, 30N_080E, 30N_090E, 20N_070E,
20N_080E and 20N_090E from
`https://storage.googleapis.com/earthenginepartners-hansen/GFC-2025-v1.13/` into
`data/raw/hansen_gfc/`, then:

```sh
.venv/bin/python -m data_tools.build_india_forest_change --max-minutes 35

COMMON="--datasets oscd,india_forest --val-every 400 --base-channels 32 --max-pos-weight 3 \
  --learning-rate 5e-4 --ema-decay 0.995 --select last --seed 42 --align-dates --normalisation per_date"
R=data/processed/runs
# Three folds over the 14 official OSCD train cities. The test cities are never involved.
.venv/bin/python -m training.train $COMMON --steps 2000 --version f0_date --run-dir $R/f0_date \
    --oscd-val-cities hongkong,bordeaux,abudhabi,aguasclaras,paris
.venv/bin/python -m training.train $COMMON --steps 2000 --version f1_date --run-dir $R/f1_date \
    --oscd-val-cities mumbai,rennes,cupertino,pisa,bercy
.venv/bin/python -m training.train $COMMON --steps 2000 --version f2_date --run-dir $R/f2_date \
    --oscd-val-cities beirut,beihai,nantes,saclay_e
# Final model: all 14 cities, fixed length, no best-step selection.
.venv/bin/python -m training.train $COMMON --steps 2500 --version change_v2 --run-dir $R/change_v2 \
    --oscd-val-cities none
cp $R/change_v2/best.pt $R/change_v2/candidate.pt
.venv/bin/python -m training.calibrate --folds $R/f0_date $R/f1_date $R/f2_date \
    --checkpoint $R/change_v2/candidate.pt --version v2 --report reports/ml/change_v2_calibration.json
.venv/bin/python -m training.evaluate export-change --run-dir $R/change_v2 --version v2 \
    --report reports/ml/change_v2_training.json
```

Then evaluate each model once per test set and record the decision:

```sh
.venv/bin/python -m training.evaluate change --checkpoint $R/change_v1/best.pt --dataset india_forest \
    --report reports/ml/change_v1_india_forest_test.json
.venv/bin/python -m training.evaluate change --checkpoint $R/change_v2/candidate.pt --dataset oscd \
    --report reports/ml/change_v2_oscd_test.json
.venv/bin/python -m training.evaluate change --checkpoint $R/change_v2/candidate.pt --dataset india_forest \
    --report reports/ml/change_v2_india_forest_test.json
.venv/bin/python -m training.select_change
```

### 4. Registry, timing, tests

```sh
cp data/processed/india_forest/summary.json reports/ml/india_forest_dataset.json
.venv/bin/python -c "from data_tools.build_india_landcover import summarise; summarise()"
cp data/processed/india_landcover/summary.json reports/ml/india_landcover_dataset.json
.venv/bin/python -m training.evaluate timing
.venv/bin/python -m training.build_registry
.venv/bin/python -m pytest tests/ml -q
.venv/bin/python -m ruff check app/ml training data_tools tests/ml
```

## Data licences

| Dataset | Use | Licence |
|---|---|---|
| OSCD (Daudt et al. 2018), mirror pinned by TorchGeo | change | Change maps CC BY-NC-SA 4.0, academic use only |
| ESA WorldCover 2021 v200 | land-cover labels | CC BY 4.0 |
| Maus et al. 2022, doi 10.1594/PANGAEA.942325 | mine class | CC BY-SA 4.0 |
| Hansen GFC-2025-v1.13 | forest-change labels | CC BY 4.0 |
| Natural Earth admin 1 | sampling only | Public domain |
| Copernicus Sentinel-2 L2A via Earth Search | imagery | Copernicus open data |

## Sentinel-2 reflectance

Earth Search lists `raster:bands.offset = -0.1` on most scenes with baseline 04.00 or later, but it
often does not apply. See `data_tools/s2_stac.py` for the measurements. With
`earthsearch:boa_offset_applied` true, pixels are already corrected and the offset must not be
applied again. With the flag false and baseline 04.00 or later, some scenes carry the +1000 digital
number offset and some do not. The dataset builders skip that ambiguous group.
