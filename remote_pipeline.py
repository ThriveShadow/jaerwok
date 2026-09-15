# remote_pipeline.py
import re
import time
from pathlib import Path
from typing import Iterator, Tuple
import paramiko

import config
from pipeline import STAGE_MARKERS, PipelineError

DOCKER_IMAGE = "opendronemap/odm"

IMPORTANT_REMOTE_PATHS = [
    "odm_orthophoto/odm_orthophoto.tif",
    "odm_report/stats.json",
    "opensfm/stats/dsm.png",
    "opensfm/stats/matchgraph.png",
]

class RemoteODMPipeline:
    def __init__(self, project_dir: Path, quality: str = "medium", max_concurrency: int = 4):
        self.project_dir = Path(project_dir)
        self.quality = quality
        self.max_concurrency = max(1, max_concurrency)
        self.ssh = None

    def _quality_flags(self):
        presets = {
            "low": ["--feature-quality", "low", "--pc-quality", "low"],
            "medium": ["--feature-quality", "medium", "--pc-quality", "medium"],
            "high": ["--feature-quality", "high", "--pc-quality", "high"],
        }
        return presets.get(self.quality, presets["medium"])

    def run(self) -> Iterator[Tuple[int, str]]:
        images = list((self.project_dir / "images").glob("*"))
        if len(images) < 3:
            raise PipelineError(f"Need at least 3-5 overlapping images, found {len(images)}.")

        yield 1, f"Connecting to remote VM at {config.REMOTE_VM_HOST}..."
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(
            hostname=config.REMOTE_VM_HOST,
            username=config.REMOTE_VM_USER,
            key_filename=config.REMOTE_VM_SSH_KEY,
            timeout=15,
        )

        remote_proj_dir = f"{config.REMOTE_VM_WORK_DIR}/{self.project_dir.name}"

        try:
            yield 5, "Uploading project images via SFTP..."
            
            # 1. Create directories and WAIT for it to finish
            _, mkdir_out, _ = self.ssh.exec_command(f"mkdir -p {remote_proj_dir}/images")
            mkdir_out.channel.recv_exit_status()
            
            sftp = self.ssh.open_sftp()
            total = len(images)
            for i, img_path in enumerate(images, start=1):
                sftp.put(str(img_path), f"{remote_proj_dir}/images/{img_path.name}")
                pct = 5 + int((i / total) * 15)
                yield pct, f"Uploaded image {i}/{total}"
            sftp.close()

            yield 20, "Starting remote Dockerized ODM worker..."
            odm_flags = " ".join([
                "--orthophoto-resolution 3",
                "--skip-3dmodel",
                "--dsm",
                f"--max-concurrency {self.max_concurrency}",
            ] + self._quality_flags())

            cmd = (
                f"docker run --rm "
                f"-v {remote_proj_dir}:/datasets/code "
                f"{DOCKER_IMAGE} "
                f"--project-path /datasets code {odm_flags}"
            )

            stdin, stdout, stderr = self.ssh.exec_command(cmd, get_pty=False)
            channel = stdout.channel
            channel.settimeout(1.0)

            last_progress = 20
            buf = ""

            while True:
                if channel.exit_status_ready() and not channel.recv_ready():
                    break
                try:
                    chunk = channel.recv(4096)
                    if not chunk:
                        continue
                    buf += chunk.decode(errors="replace")
                    
                    while "\n" in buf or "\r" in buf:
                        n_idx, r_idx = buf.find("\n"), buf.find("\r")
                        split_idx = min(n_idx, r_idx) if n_idx != -1 and r_idx != -1 else max(n_idx, r_idx)
                        line = buf[:split_idx].strip()
                        buf = buf[split_idx + 1:]
                        if not line:
                            continue

                        for pattern, pct, label in STAGE_MARKERS:
                            if re.search(pattern, line, re.IGNORECASE):
                                scaled = 20 + int((pct / 97) * 65)
                                if scaled > last_progress:
                                    last_progress = scaled
                                    yield scaled, label
                                break
                except TimeoutError:
                    pass

            # 2. Check if ODM actually succeeded before proceeding
            exit_status = channel.recv_exit_status()
            if exit_status != 0:
                # Capture the actual error output
                error_log = stderr.read().decode(errors="replace").strip()
                raise PipelineError(f"Remote ODM crashed (Exit code {exit_status}). Remote error: {error_log}")

            # 3. Fix permissions and WAIT for it to finish
            yield 88, "Fixing remote file permissions..."
            _, chown_out, _ = self.ssh.exec_command(
                f"docker run --rm -v {remote_proj_dir}:/datasets/code alpine "
                f"chown -R $(id -u):$(id -g) /datasets/code"
            )
            chown_out.channel.recv_exit_status()

            yield 92, "Downloading results from remote VM..."
            sftp = self.ssh.open_sftp()
            for rel_path in IMPORTANT_REMOTE_PATHS:
                r_file = f"{remote_proj_dir}/{rel_path}"
                l_file = self.project_dir / rel_path
                l_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    sftp.get(r_file, str(l_file))
                except (FileNotFoundError, IOError):
                    pass
            sftp.close()

            yield 99, "Orthomosaic generated successfully on remote worker"

        finally:
            if self.ssh:
                # Clean up remote project working directory
                self.ssh.exec_command(f"rm -rf {remote_proj_dir}")
                self.ssh.close()