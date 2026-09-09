"""
Tea Plantation Mapping Web App
Step 1: Upload images
Step 2: Reconstruct orthomosaic (OpenSfM + OpenMVS + orthophoto, via ODM/OpenDroneMap)
Step 3: YOLOv8 detection (stub, not implemented yet)

Run with:
    python app.py
Then open http://<vm-ip>:5000
"""
import json
import os
import shutil
import threading
import time
import uuid

from functools import wraps
from flask import session
import config

from pathlib import Path

from datetime import datetime

from flask import Flask, jsonify, render_template, request, send_from_directory, url_for, redirect
from werkzeug.utils import secure_filename

from pipeline import ODMPipeline, PipelineError
from report_gen import build_report

from ngrdi import NGRDIProcessor, NGRDIError

BASE_DIR = Path(__file__).resolve().parent
PROJECTS_DIR = BASE_DIR / "projects"
PROJECTS_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
MAX_CONTENT_LENGTH = 4 * 1024 * 1024 * 1024  # 4 GB total upload cap, adjust as needed

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

app.secret_key = config.SECRET_KEY


@app.before_request
def require_login():
    if request.endpoint in ("login", "static"):
        return
    if not session.get("logged_in"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if (request.form.get("username") == config.WEB_USERNAME
                and request.form.get("password") == config.WEB_PASSWORD):
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Invalid username or password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))

# In-memory job registry: {project_id: {"status": ..., "progress": ..., "log": [...], "error": ...}}
JOBS = {}
JOBS_LOCK = threading.Lock()

# In-memory job registry for the NGRDI step: {project_id: {...}}
NGRDI_JOBS = {}
NGRDI_JOBS_LOCK = threading.Lock()


def project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def images_dir(project_id: str) -> Path:
    return project_dir(project_id) / "images"


def is_allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Step 1: Upload
# ---------------------------------------------------------------------------
@app.route("/api/upload", methods=["POST"])
def upload():
    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "No files received"}), 400

    project_id = request.form.get("project_id") or uuid.uuid4().hex[:12]
    img_dir = images_dir(project_id)
    img_dir.mkdir(parents=True, exist_ok=True)

    saved, skipped = [], []
    for f in files:
        if not f.filename:
            continue
        if not is_allowed(f.filename):
            skipped.append(f.filename)
            continue
        safe_name = secure_filename(f.filename)
        dest = img_dir / safe_name
        f.save(dest)
        saved.append(safe_name)

    with JOBS_LOCK:
        JOBS.setdefault(project_id, {"status": "uploaded", "progress": 0, "log": [], "error": None})

    return jsonify({
        "project_id": project_id,
        "saved_count": len(saved),
        "skipped": skipped,
        "total_images": len(list(img_dir.glob("*"))),
    })

@app.route("/api/project/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    pdir = project_dir(project_id)
    if not pdir.exists():
        return jsonify({"error": "unknown project"}), 404

    with JOBS_LOCK:
        job = JOBS.get(project_id)
        if job and job.get("status") == "running":
            return jsonify({"error": "cannot delete while reconstruction is running"}), 409
    with NGRDI_JOBS_LOCK:
        ngrdi_job = NGRDI_JOBS.get(project_id)
        if ngrdi_job and ngrdi_job.get("status") == "running":
            return jsonify({"error": "cannot delete while NGRDI analysis is running"}), 409

    try:
        shutil.rmtree(pdir)
    except OSError as e:
        return jsonify({"error": f"could not delete project files: {e}"}), 500

    with JOBS_LOCK:
        JOBS.pop(project_id, None)
    with NGRDI_JOBS_LOCK:
        NGRDI_JOBS.pop(project_id, None)

    return jsonify({"deleted": project_id})


@app.route("/api/project/<project_id>/images", methods=["GET"])
def list_images(project_id):
    img_dir = images_dir(project_id)
    if not img_dir.exists():
        return jsonify({"error": "unknown project"}), 404
    names = sorted(p.name for p in img_dir.glob("*") if is_allowed(p.name))
    return jsonify({"project_id": project_id, "count": len(names), "images": names})


@app.route("/api/project/<project_id>/images/<filename>", methods=["DELETE"])
def delete_image(project_id, filename):
    target = images_dir(project_id) / secure_filename(filename)
    if target.exists():
        target.unlink()
        return jsonify({"deleted": filename})
    return jsonify({"error": "not found"}), 404


# ---------------------------------------------------------------------------
# Step 2: Reconstruction (OpenSfM -> OpenMVS -> Orthophoto, via ODM)
# ---------------------------------------------------------------------------
@app.route("/api/process/<project_id>", methods=["POST"])
def process(project_id):
    img_dir = images_dir(project_id)
    if not img_dir.exists() or not any(img_dir.iterdir()):
        return jsonify({"error": "no images uploaded for this project"}), 400

    with JOBS_LOCK:
        job = JOBS.get(project_id)
        if job and job["status"] == "running":
            return jsonify({"error": "already running"}), 409
        JOBS[project_id] = {"status": "running", "progress": 1, "log": ["queued"], "error": None,
                             "started_at": time.time()}

    opts = request.get_json(silent=True) or {}
    quality = opts.get("quality", "medium")
    max_concurrency = int(opts.get("max_concurrency", max(1, os.cpu_count() - 1)))
    compute = opts.get("compute", "local")  # "local" | "runpod"

    def _run():
        if compute == "runpod":
            from runpod_pipeline import RunPodODMPipeline, RunPodPipelineError
            pipeline = RunPodODMPipeline(
                project_dir=project_dir(project_id),
                quality=quality,
                max_concurrency=max_concurrency,
            )
        else:
            pipeline = ODMPipeline(
                project_dir=project_dir(project_id),
                quality=quality,
                max_concurrency=max_concurrency,
            )
        try:
            for progress, message in pipeline.run():
                with JOBS_LOCK:
                    JOBS[project_id]["progress"] = progress
                    ts = datetime.now().strftime("%H:%M:%S")
                    JOBS[project_id]["log"].append(f"[{ts}] {message}")
            build_report(project_dir(project_id))
            with JOBS_LOCK:
                JOBS[project_id]["status"] = "done"
                JOBS[project_id]["progress"] = 100
                JOBS[project_id]["finished_at"] = time.time()
        except PipelineError as e:
            with JOBS_LOCK:
                JOBS[project_id]["status"] = "error"
                JOBS[project_id]["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            with JOBS_LOCK:
                JOBS[project_id]["status"] = "error"
                JOBS[project_id]["error"] = f"unexpected error: {e}"

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return jsonify({"project_id": project_id, "status": "running", "compute": compute})

@app.route("/api/status/<project_id>", methods=["GET"])
def status(project_id):
    with JOBS_LOCK:
        job = JOBS.get(project_id)
    if not job:
        return jsonify({"error": "unknown project"}), 404
    return jsonify(job)


# ---------------------------------------------------------------------------
# Step 2 results: orthomosaic + report
# ---------------------------------------------------------------------------
@app.route("/api/result/<project_id>", methods=["GET"])
def result(project_id):
    pdir = project_dir(project_id)
    report_path = pdir / "report" / "report.json"
    if not report_path.exists():
        return jsonify({"error": "report not ready"}), 404
    with open(report_path) as f:
        data = json.load(f)
    data["preview_url"] = url_for("serve_artifact", project_id=project_id, filename="report/orthomosaic_preview.png")
    data["orthophoto_download_url"] = url_for("serve_artifact", project_id=project_id, filename="odm_orthophoto/odm_orthophoto.tif")
    data["pdf_download_url"] = url_for("serve_artifact", project_id=project_id, filename="report/report.pdf")
    return jsonify(data)


@app.route("/files/<project_id>/<path:filename>")
def serve_artifact(project_id, filename):
    pdir = project_dir(project_id)
    return send_from_directory(pdir, filename)

@app.route("/api/projects", methods=["GET"])
def list_projects_metadata():
    projs = []
    if PROJECTS_DIR.exists():
        for p in PROJECTS_DIR.iterdir():
            if p.is_dir():
                preview_path = p / "report" / "orthomosaic_preview.png"
                projs.append({
                    "id": p.name,
                    "has_preview": preview_path.exists(),
                    "preview_url": url_for("serve_artifact", project_id=p.name, filename="report/orthomosaic_preview.png") if preview_path.exists() else None
                })
    return jsonify({"projects": projs})

@app.route("/api/report/regenerate/<project_id>", methods=["POST"])
def regenerate_report(project_id):
    pdir = project_dir(project_id)
    if not pdir.exists():
        return jsonify({"error": "unknown project"}), 404
    try:
        data = build_report(pdir)
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# Step 3: NGRDI vegetation index
# ---------------------------------------------------------------------------
@app.route("/api/ngrdi/<project_id>", methods=["POST"])
def ngrdi_process(project_id):
    pdir = project_dir(project_id)
    ortho = pdir / "odm_orthophoto" / "odm_orthophoto.tif"
    if not ortho.exists():
        return jsonify({"error": "run orthomosaic reconstruction first"}), 400

    with NGRDI_JOBS_LOCK:
        job = NGRDI_JOBS.get(project_id)
        if job and job["status"] == "running":
            return jsonify({"error": "already running"}), 409
        NGRDI_JOBS[project_id] = {"status": "running", "progress": 1, "log": ["queued"], "error": None}

    opts = request.get_json(silent=True) or {}
    vmin = float(opts.get("vmin", -0.05))
    vmax = float(opts.get("vmax", 0.2))

    def _run():
        processor = NGRDIProcessor(project_dir(project_id), vmin=vmin, vmax=vmax)
        try:
            for progress, message in processor.run():
                with NGRDI_JOBS_LOCK:
                    NGRDI_JOBS[project_id]["progress"] = progress
                    ts = datetime.now().strftime("%H:%M:%S")
                    NGRDI_JOBS[project_id]["log"].append(f"[{ts}] {message}")
            build_report(project_dir(project_id))  # regenerate PDF to include NGRDI section
            with NGRDI_JOBS_LOCK:
                NGRDI_JOBS[project_id]["status"] = "done"
                NGRDI_JOBS[project_id]["progress"] = 100
        except NGRDIError as e:
            with NGRDI_JOBS_LOCK:
                NGRDI_JOBS[project_id]["status"] = "error"
                NGRDI_JOBS[project_id]["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            with NGRDI_JOBS_LOCK:
                NGRDI_JOBS[project_id]["status"] = "error"
                NGRDI_JOBS[project_id]["error"] = f"unexpected error: {e}"

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return jsonify({"project_id": project_id, "status": "running"})


@app.route("/api/ngrdi/status/<project_id>", methods=["GET"])
def ngrdi_status(project_id):
    with NGRDI_JOBS_LOCK:
        job = NGRDI_JOBS.get(project_id)
    if not job:
        return jsonify({"error": "unknown project"}), 404
    return jsonify(job)


@app.route("/api/ngrdi/result/<project_id>", methods=["GET"])
def ngrdi_result(project_id):
    pdir = project_dir(project_id)
    meta_path = pdir / "ngrdi" / "ngrdi_report.json"
    if not meta_path.exists():
        return jsonify({"error": "NGRDI result not ready"}), 404
    with open(meta_path) as f:
        data = json.load(f)
    data["preview_url"] = url_for("serve_artifact", project_id=project_id, filename="ngrdi/ngrdi_preview.png")
    data["raw_download_url"] = url_for("serve_artifact", project_id=project_id, filename=f"ngrdi/{data['raw_geotiff']}")
    data["colored_download_url"] = url_for("serve_artifact", project_id=project_id, filename=f"ngrdi/{data['colored_geotiff']}")
    return jsonify(data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True, threaded=True)
