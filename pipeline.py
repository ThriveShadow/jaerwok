import re
import shlex
import subprocess
import threading
import time
import json
import psutil
import os
from pathlib import Path
from typing import Iterator, Tuple

DOCKER_IMAGE = "opendronemap/odm"
STAGE_MARKERS = [
    (r"dataset\s+stage", 5, "Loading dataset"),
    (r"opensfm\s+stage", 10, "Running OpenSfM: feature extraction & matching"),
    (r"detect_features", 15, "OpenSfM: detecting features"),
    (r"match_features", 20, "OpenSfM: matching features across images"),
    (r"create_tracks", 25, "OpenSfM: building tracks"),
    (r"reconstruct", 35, "OpenSfM: sparse reconstruction (camera poses)"),
    (r"undistort", 40, "OpenSfM: undistorting images"),
    (r"openmvs\s+stage|compute depthmaps", 55, "OpenMVS: computing depth maps (dense point cloud)"),
    (r"odm_filterpoints|filter points", 60, "Filtering point cloud"),
    (r"odm_meshing|meshing stage", 65, "Building mesh"),
    (r"odm_dem|dem stage|creating dem", 75, "Generating DEM"),
    (r"odm_orthophoto|orthophoto stage", 90, "Rendering orthomosaic (GDAL ortho-rectification)"),
    (r"odm_report|report stage", 97, "Writing ODM report"),
]

class PipelineError(RuntimeError):
    pass

def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

class ResourceMonitor:
    def __init__(self, log_file: Path):
        self.log_file = log_file
        self.running = False
        self.data = {"time": [], "cpu": [], "ram": [], "markers": []}
        self.thread = None
        self.start_t = None

    def start(self):
        self.start_t = time.time()
        self.running = True
        self.thread = threading.Thread(target=self._run)
        self.thread.start()

    def record_event(self, label: str):
        if self.start_t:
            self.data["markers"].append({"time": time.time() - self.start_t, "label": label})

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_file, "w") as f:
            json.dump(self.data, f)

    def _run(self):
        while self.running:
            self.data["time"].append(time.time() - self.start_t)
            self.data["cpu"].append(psutil.cpu_percent(interval=None))
            self.data["ram"].append(psutil.virtual_memory().percent)
            time.sleep(5)

class ODMPipeline:
    def __init__(self, project_dir: Path, quality: str = "medium", max_concurrency: int = 4):
        self.project_dir = Path(project_dir)
        self.quality = quality
        self.max_concurrency = max(1, max_concurrency)

    def _quality_flags(self):
        presets = {
            "low": ["--feature-quality", "low", "--pc-quality", "low"],
            "medium": ["--feature-quality", "medium", "--pc-quality", "medium"],
            "high": ["--feature-quality", "high", "--pc-quality", "high"],
        }
        return presets.get(self.quality, presets["medium"])

    def _build_command(self):
        host_path = str(self.project_dir.resolve())
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{host_path}:/datasets/code",
            DOCKER_IMAGE,
            "--project-path", "/datasets",
            "--max-concurrency", str(self.max_concurrency),
            "--orthophoto-resolution", "3",
            "--skip-3dmodel",
            "--dsm",
        ] + self._quality_flags()
        return cmd

    def _fix_ownership(self):
        """ODM's container writes output as root. Reclaim it for the host
        user with a disposable container, so cleanup/delete works normally."""
        host_path = str(self.project_dir.resolve())
        try:
            subprocess.run(
                ["docker", "run", "--rm",
                 "-v", f"{host_path}:/datasets/code",
                 "alpine", "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/datasets/code"],
                check=True, capture_output=True,
            )
        except Exception:
            pass  # best-effort; a stuck file will still surface a clear error on delete

    def run(self) -> Iterator[Tuple[int, str]]:
        if not _docker_available():
            raise PipelineError("Docker is not available.")

        images = list((self.project_dir / "images").glob("*"))
        if len(images) < 3:
            raise PipelineError(f"Need at least 3-5 overlapping images, found {len(images)}.")

        cmd = self._build_command()
        yield 2, f"Starting ODM: {' '.join(shlex.quote(c) for c in cmd)}"
        
        # Start CPU/RAM Monitor
        monitor = ResourceMonitor(self.project_dir / "report" / "usage.json")
        monitor.start()
        monitor.record_event("Starting ODM")

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            last_progress = 2
            for line in proc.stdout:
                line = line.rstrip()
                if not line: continue
                for pattern, pct, label in STAGE_MARKERS:
                    if re.search(pattern, line, re.IGNORECASE):
                        if pct > last_progress:
                            last_progress = pct
                            monitor.record_event(label)
                            yield pct, label
                        break
                else:
                    if re.search(r"error|exception|traceback", line, re.IGNORECASE):
                        yield last_progress, line

            returncode = proc.wait()
            if returncode != 0:
                raise PipelineError(f"ODM exited with code {returncode}.")

            ortho = self.project_dir / "odm_orthophoto" / "odm_orthophoto.tif"
            if not ortho.exists():
                raise PipelineError("No odm_orthophoto.tif produced.")
            
            monitor.record_event("Orthomosaic generated")
            yield 99, "Orthomosaic generated"
        finally:
            monitor.stop()
            self._fix_ownership()