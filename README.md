# Tea Plantation Mapping — Step 1 & 2

Web app for: **upload images -> orthomosaic reconstruction**. Step 3 (YOLOv8) is a UI
placeholder only for now.

## Why this uses ODM, not raw OpenSfM

OpenSfM by itself only gets you camera poses and a sparse/dense point cloud — it does
not ortho-rectify anything into a mosaic. Producing an actual orthomosaic needs a DEM
and GDAL-based ortho-rectification on top of that, which is what
[OpenDroneMap (ODM)](https://github.com/OpenDroneMap/ODM) provides. ODM runs OpenSfM
internally as its first stage, then OpenMVS for dense reconstruction, then DEM +
orthophoto generation. This app drives ODM's Docker image and reports on all of it —
so "OpenSfM" in your pipeline diagram maps to the first ~40% of the step 2 progress
bar here.

## VM setup (one-time)

```bash
# 1. Docker (runs the ODM reconstruction pipeline)
sudo apt update
sudo apt install -y docker.io gdal-bin
sudo usermod -aG docker $USER
newgrp docker   # or log out/in

# 2. Pull the ODM image (~2-3GB, do this once ahead of time so the first
#    reconstruction job doesn't stall on a slow pull)
docker pull opendronemap/odm

# 3. Python deps for the web app itself
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`gdal-bin` gives the app `gdal_translate`, used to make a quick PNG preview of the
orthomosaic GeoTIFF for the browser. If it's missing, the app falls back to Pillow,
which works for most ODM outputs but ignores georeferencing for the preview only
(the downloadable GeoTIFF is unaffected either way).

## Run

```bash
source venv/bin/activate
python app.py
```

Open `http://<vm-ip>:5000` (or `http://localhost:5000` if you're on the VM's desktop).

## Tuning for 24GB RAM / no GPU

Dense reconstruction (the OpenMVS stage) is what eats RAM, not OpenSfM's sparse stage.
In the step 2 UI:

- **Quality**: start with `medium`. Only try `high` on small batches (under ~50
  images) — it can spike RAM well past 24GB on larger sets and get OOM-killed.
- **Resize to**: 2048px is a reasonable default for tea-canopy-scale detail. Drop to
  1600 or 1200 if a run OOMs.
- **Max concurrency**: defaults to `cpu_count - 1`. Lower this if you're running other
  things on the VM at the same time — it trades wall-clock time for peace, not RAM
  headroom (each thread does still add memory pressure though).

If a job gets killed, check `dmesg | grep -i "killed process"` — that confirms OOM
rather than a pipeline bug, and the fix is quality/resize, not code.

## What step 2 actually needs from your images

- **Overlap**: 70–80% forward, 60%+ side overlap between neighboring frames. Below
  that, OpenSfM can't find enough matching features and reconstruction fails outright
  or produces a fragmented/incomplete orthomosaic.
- **Minimum count**: the app blocks reconstruction under 3 images, but realistically
  you want dozens+ for a plantation-scale mosaic with real overlap.
- Geotagged EXIF (GPS) on the images will make ODM's job much easier and faster —
  most drone flight-planning apps do this by default. If images aren't geotagged, ODM
  can still work from feature matching alone but is slower and less reliable on
  repetitive textures (tea canopy rows are exactly this kind of repetitive texture,
  so geotags matter more here than on varied terrain).

## Output layout

```
projects/<project_id>/
  images/                     # what you uploaded
  odm_orthophoto/odm_orthophoto.tif   # the orthomosaic (GeoTIFF, real download)
  odm_report/stats.json       # ODM's own reconstruction stats
  report/report.json          # summary this app parses out for the UI
  report/orthomosaic_preview.png      # downsampled preview shown in-browser
```

## Known rough edges to expect

- ODM's `stats.json` schema has shifted across versions; `report_gen.py` reads it
  defensively but some fields may show `-` in the UI depending on your installed ODM
  image version. Worth checking `odm_report/stats.json` directly if a field you care
  about is missing.
- No auth, no queueing beyond one job per project — fine for a single-user VM
  workflow, not meant to be exposed to the internet as-is.
- Large uploads (drone batches easily hit several GB) — `MAX_CONTENT_LENGTH` in
  `app.py` is set to 4GB, raise it if needed.
