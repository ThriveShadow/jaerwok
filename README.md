# Tea Plantation Mapping — Steps 1–3

Web app for: **upload images → orthomosaic reconstruction → NGRDI vegetation index**. Step 4 (YOLOv8 detection) is a UI placeholder only, not implemented yet.

## Pipeline overview

1. **Upload** — drone/UAV images go into `projects/<project_id>/images/`.
2. **Reconstruction (ODM)** — OpenSfM (feature matching + sparse reconstruction) → OpenMVS (dense reconstruction) → GDAL ortho-rectification, producing a georeferenced orthomosaic GeoTIFF. Can run **locally** via Docker, or **on RunPod** (cloud CPU pod) for faster/heavier jobs off the VM.
3. **NGRDI** — vegetation index computed from the orthomosaic; produces a raw float32 GeoTIFF, a colorized (red-yellow-green) GeoTIFF, and a preview PNG.
4. **YOLOv8 detection** — not wired up yet.

### Why ODM, not raw OpenSfM

OpenSfM alone only gets you camera poses and a point cloud — it doesn't ortho-rectify anything into a mosaic. Producing an actual orthomosaic needs a DEM and GDAL-based ortho-rectification on top of that, which is what [OpenDroneMap (ODM)](https://github.com/OpenDroneMap/ODM) provides. ODM runs OpenSfM internally as its first stage, so "OpenSfM" in a pipeline diagram maps to roughly the first third of the step-2 progress bar.

## Compute modes

The app supports two ways to run reconstruction, chosen per-job from the UI (`opt-compute`):

- **Local VM (Docker)** — runs ODM's Docker image directly on the host VM. Simple, no external dependencies, bounded by the VM's own CPU/RAM.
- **RunPod (cloud)** — provisions a temporary RunPod CPU pod sized to the job's quality setting, uploads images to a RunPod network volume, runs ODM over SSH on the pod, pulls back only the important result files (orthophoto, stats, preview images), then tears the pod down. Useful when the VM itself is small/shared and you want faster or heavier jobs without permanently provisioning bigger hardware.

### RunPod setup (one-time, only needed if using the RunPod compute mode)

```bash
# generate an SSH keypair if you don't already have one, and register
# the PUBLIC key in your RunPod account (Settings -> SSH Keys)
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
```

Set the following in `config.py` (or your env, depending on how `config` loads them):

| Variable | Purpose |
|---|---|
| `RUNPOD_API_KEY` | RunPod REST API key |
| `RUNPOD_SSH_PRIVATE_KEY_PATH` | Path to the private key matching the pubkey registered on RunPod |
| `RUNPOD_VOLUME_SIZE_GB` | Size of the network volume created per job |
| `RUNPOD_VOLUME_DATACENTER` | Datacenter ID the volume/pod are provisioned in |
| `RUNPOD_CLOUD_TYPE` | e.g. `COMMUNITY` |
| `RUNPOD_MAX_RUNTIME_MIN` | Hard auto-kill timer baked into the pod's boot script, in case a job or the SSH connection hangs |
| `RUNPOD_S3_ENDPOINT`, `RUNPOD_S3_ACCESS_KEY`, `RUNPOD_S3_SECRET_KEY` | S3-compatible credentials for uploading images to the network volume ahead of pod creation |

**Known rough edge:** RunPod network volumes are backed by a shared MooseFS/FUSE filesystem. A freshly-created volume can be noticeably slower for many-small-file access than an already-"warm" one, and this can look like the pipeline is stuck during ODM's dataset-loading stage on larger image sets (100+ images). If a RunPod job seems frozen right after "Loading dataset," it's very likely this, not a crash — check `ps aux` on the pod over SSH before assuming something's broken.

## VM setup (one-time)

```bash
# 1. Docker (needed for local compute mode)
sudo apt update
sudo apt install -y docker.io gdal-bin
sudo usermod -aG docker $USER
newgrp docker   # or log out/in

# 2. Pull the ODM image (~2-3GB, do this once ahead of time so the first
#    local-mode reconstruction job doesn't stall on a slow pull)
docker pull opendronemap/odm

# 3. Python deps for the web app itself
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`gdal-bin` gives the app `gdal_translate`, used to make a quick PNG preview of the orthomosaic GeoTIFF for the browser. If it's missing, the app falls back to Pillow, which works for most ODM outputs but ignores georeferencing for the preview only (the downloadable GeoTIFF is unaffected either way).

## Auth

The app requires login (username/password set via `config.WEB_USERNAME` / `config.WEB_PASSWORD`, session-based). There's no per-user separation — this is a single shared login gate, not multi-tenant auth.

## Run

```bash
source venv/bin/activate
python app.py
```

Open `http://<vm-ip>:5050` (or `http://localhost:5050` if you're on the VM's desktop). You'll be redirected to a login page first.

## Using the app

- **Existing Projects** grid on the landing step shows all previously created projects, with previews where available. Clicking a card re-opens that project — if reconstruction is still running, the UI resumes polling instead of assuming it's done; if it's finished, results load directly.
- **+ New Project** reveals the upload dropzone and clears any previous project's state, so a fresh upload always gets a new `project_id` from the server instead of accidentally appending to whatever project you last had open.
- Uploads go out with limited concurrency and per-file progress; the very first file of a batch establishes the `project_id`, subsequent files in that batch reuse it.
- Step 2's progress bar and log are polled every 2s while a job is `running`, and reflect the same in-memory job state whether the job just started or you're re-opening a project mid-run.
- **Regenerate Report** re-derives `report/report.json` (and the PDF) from whatever result files currently exist on disk — useful if a job's raw ODM output is present but the report step itself didn't run (e.g. after manually recovering files from a RunPod pod that a job lost track of).

## What step 2 actually needs from your images

- **Overlap**: 70–80% forward, 60%+ side overlap between neighboring frames. Below that, OpenSfM can't find enough matching features and reconstruction fails outright or produces a fragmented/incomplete orthomosaic.
- **Minimum count**: the app blocks reconstruction under 3 images, but realistically you want dozens+ for a plantation-scale mosaic with real overlap.
- Geotagged EXIF (GPS) on the images will make ODM's job much easier and faster — most drone flight-planning apps do this by default. If images aren't geotagged, ODM can still work from feature matching alone but is slower and less reliable on repetitive textures (tea canopy rows are exactly this kind of repetitive texture, so geotags matter more here than on varied terrain).

## Tuning for local compute mode on limited RAM / no GPU

Dense reconstruction (the OpenMVS stage) is what eats RAM, not OpenSfM's sparse stage. In the step 2 UI:

- **Quality**: start with `medium`. Only try `high` on small batches — it can spike RAM well past what a modest VM has and get OOM-killed.
- **Max concurrency**: defaults to `cpu_count - 1`. Lower this if you're running other things on the VM at the same time — it trades wall-clock time for peace, not RAM headroom (each thread does still add memory pressure though).

If a local-mode job gets killed, check `dmesg | grep -i "killed process"` — that confirms OOM rather than a pipeline bug, and the fix is quality/settings, not code. (For RunPod-mode jobs, check the pod's `/sys/fs/cgroup/memory.events` instead, since `dmesg` isn't accessible from inside an unprivileged container.)

## Output layout

```
projects/<project_id>/
  images/                              # what you uploaded
  odm_orthophoto/odm_orthophoto.tif    # the orthomosaic (GeoTIFF, real download)
  odm_report/stats.json                # ODM's own reconstruction stats
  report/report.json                   # summary this app parses out for the UI
  report/report.pdf                    # full PDF report
  report/orthomosaic_preview.png       # downsampled preview shown in-browser
  ngrdi/
    ngrdi_report.json                  # NGRDI run metadata parsed for the UI
    ngrdi_preview.png                  # preview shown in-browser
    <raw NGRDI GeoTIFF, float32>
    <colorized NGRDI GeoTIFF, RGB>
```

## Known rough edges to expect

- ODM's `stats.json` schema has shifted across versions; `report_gen.py` reads it defensively but some fields may show `-` in the UI depending on your installed ODM image version. Worth checking `odm_report/stats.json` directly if a field you care about is missing.
- Job status (`JOBS` / `NGRDI_JOBS`) lives in-memory in the Flask process. A server restart while a job is running loses track of it — the job may still finish (or already have finished) on the compute side, but the UI/API will show it as unknown until you manually reconcile (see "Regenerate Report" above, or pull results directly via SSH/SCP for RunPod-mode jobs).
- No queueing beyond one reconstruction job per project at a time; a second `/api/process` call on an already-running project is rejected with 409.
- No per-user separation — one shared login gate, not meant to be exposed to the internet as-is beyond that basic auth.
- Large uploads (drone batches easily hit several GB) — `MAX_CONTENT_LENGTH` in `app.py` is set to 4GB, raise it if needed.