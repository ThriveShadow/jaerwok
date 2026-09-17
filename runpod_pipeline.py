#runpod_pipeline.py
import io
import re
import tarfile
import time
from pathlib import Path
from typing import Iterator, Tuple
import shlex

import threading

import boto3
from botocore.config import Config as BotoConfig
from boto3.s3.transfer import TransferConfig

import subprocess
import paramiko
import requests

import config
from pipeline import STAGE_MARKERS, PipelineError
from runpod_client import RunPodClient, RunPodError

from concurrent.futures import ThreadPoolExecutor, as_completed


DOCKER_IMAGE = "thriveshadow/odm-runpod:latest"

QUALITY_VCPU = {"low": 8, "medium": 16, "high": 32}

# Only these are pulled back from the pod - keeps the download small and
# fast instead of dragging back every intermediate point cloud / mesh file.
IMPORTANT_REMOTE_PATHS = [
    "odm_orthophoto/odm_orthophoto.tif",
    "odm_report/stats.json",
    "opensfm/stats/dsm.png",
    "opensfm/stats/matchgraph.png",
]


class RunPodPipelineError(PipelineError):
    pass


class RunPodODMPipeline:
    def __init__(self, project_dir: Path, quality: str = "medium", max_concurrency: int = 4):
        self.project_dir = Path(project_dir)
        self.quality = quality
        self.max_concurrency = max(1, max_concurrency)
        self.client = RunPodClient(config.RUNPOD_API_KEY)
        self.pod_id = None
        self.volume_id = None
        self.ssh = None

    def _quality_flags(self):
        presets = {
            "low": ["--feature-quality", "low", "--pc-quality", "low"],
            "medium": ["--feature-quality", "medium", "--pc-quality", "medium"],
            "high": ["--feature-quality", "high", "--pc-quality", "high"],
        }
        return presets.get(self.quality, presets["medium"])

    def _get_volume_usage(self):
        s3 = self._s3_client()
        paginator = s3.get_paginator("list_objects_v2")
        total_bytes = 0
        count = 0
        for page in paginator.paginate(Bucket=self.volume_id):
            for obj in page.get("Contents", []):
                total_bytes += obj["Size"]
                count += 1
        return total_bytes, count

    def _s3_client(self):
        return boto3.client(
            "s3",
            endpoint_url=config.RUNPOD_S3_ENDPOINT,
            region_name=config.RUNPOD_VOLUME_DATACENTER,
            aws_access_key_id=config.RUNPOD_S3_ACCESS_KEY,
            aws_secret_access_key=config.RUNPOD_S3_SECRET_KEY,
            config=BotoConfig(
                signature_version="s3v4",
                max_pool_connections=32,  # >= your ThreadPoolExecutor max_workers
            ),
        )

    def _pick_spec(self, n_images: int):
        vcpu = QUALITY_VCPU.get(self.quality, 8)
        disk_gb = 10
        return vcpu, disk_gb

    def _local_public_key(self) -> str:
        pub_path = Path(config.RUNPOD_SSH_PRIVATE_KEY_PATH + ".pub")
        if not pub_path.exists():
            raise RunPodPipelineError(
                f"No SSH public key found at {pub_path}. Generate one with "
                f"`ssh-keygen -t ed25519` and register the public key in your RunPod account."
            )
        return pub_path.read_text().strip()

    def _boot_script(self) -> str:
        return (
            "set -e; "
            "exec > /var/log/boot.log 2>&1; "
            # Inject the public key
            'echo "$SSH_PUBLIC_KEY" > /root/.ssh/authorized_keys; '
            "chmod 600 /root/.ssh/authorized_keys; "
            # Start SSH (host keys and directories already exist in the image)
            "/usr/sbin/sshd; "
            "sleep 3; "
            # Setup the auto-kill timer
            '( sleep "$MAX_RUNTIME_S"; '
            'curl -s -X DELETE -H "Authorization: Bearer $RUNPOD_API_KEY" '
            '"https://api.runpod.io/v2/pods/$RUNPOD_POD_ID" ) & '
            # Keep container alive
            "tail -f /dev/null"
        )

    def run(self) -> Iterator[Tuple[int, str]]:
        images = list((self.project_dir / "images").glob("*"))
        if len(images) < 3:
            raise RunPodPipelineError(f"Need at least 3-5 overlapping images, found {len(images)}.")

        pub_key = self._local_public_key()
        vcpu, disk_gb = self._pick_spec(len(images))

        env = {
            "SSH_PUBLIC_KEY": pub_key,
            "RUNPOD_API_KEY": config.RUNPOD_API_KEY,
            "MAX_RUNTIME_S": str(config.RUNPOD_MAX_RUNTIME_MIN * 60),
        }

        try:
            yield 0, f"Provisioning {config.RUNPOD_VOLUME_SIZE_GB}GB network volume..."
            volume = self.client.create_network_volume(
                name=f"jaerwok-{self.project_dir.name}",
                size_gb=config.RUNPOD_VOLUME_SIZE_GB,
                data_center_id=config.RUNPOD_VOLUME_DATACENTER,
            )
            self.volume_id = volume["id"]

            yield 1, "Uploading images to network volume (no compute billing yet)"
            yield from self._upload_images_to_volume()

            yield 4, f"Creating RunPod pod (>= {vcpu} vCPU) with volume attached..."
            pod_gen = self.client.create_pod_with_fallback(
                name=f"jaerwok-{self.project_dir.name}",
                image_name=DOCKER_IMAGE,
                min_vcpu=vcpu,
                container_disk_gb=disk_gb,
                env=env,
                docker_start_cmd=[self._boot_script()],
                cloud_type=config.RUNPOD_CLOUD_TYPE,
                data_center_ids=[config.RUNPOD_VOLUME_DATACENTER],
                network_volume_id=self.volume_id,
            )
            pod = None
            while True:
                try:
                    status_msg = next(pod_gen)
                    yield 4, status_msg
                except StopIteration as si:
                    pod = si.value
                    break

            self.pod_id = pod["id"]
            yield 6, f"Pod {self.pod_id} created, waiting for boot + SSH"
            ip, port = self.client.wait_for_ssh(self.pod_id, timeout_s=300)
            yield 10, f"SSH available at {ip}:{port}, connecting"

            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._connect_with_retry(ip, port)

            yield 25, "Starting ODM on RunPod"
            yield from self._run_remote_odm()

            yield 90, "Downloading results (orthophoto, stats, previews only)"
            self._download_important_files()

            yield 99, "Orthomosaic generated on RunPod"

        except (RunPodError, requests.exceptions.RequestException) as e:
            raise RunPodPipelineError(str(e)) from e
        finally:
            ssh_to_close = self.ssh
            pod_id_to_terminate = self.pod_id
            volume_id_to_delete = self.volume_id

            def _cleanup_in_background():
                if ssh_to_close:
                    try:
                        ssh_to_close.close()
                    except Exception:
                        pass
                if pod_id_to_terminate:
                    try:
                        self.client.terminate_pod(pod_id_to_terminate)
                    except Exception as e:
                        print(f"[runpod_pipeline] WARNING: failed to terminate pod {pod_id_to_terminate}: {e}")
                if volume_id_to_delete:
                    for attempt in range(6):
                        try:
                            self.client.delete_network_volume(volume_id_to_delete)
                            break
                        except Exception as e:
                            if attempt == 5:
                                print(f"[runpod_pipeline] WARNING: failed to delete volume {volume_id_to_delete} after retries: {e}")
                            else:
                                time.sleep(5)

            threading.Thread(target=_cleanup_in_background, daemon=True).start()

    def _connect_with_retry(self, ip, port, attempts=30, delay=10):
        last_err = None
        for i in range(attempts):
            try:
                self.ssh.connect(
                    hostname=ip, port=port, username="root",
                    key_filename=config.RUNPOD_SSH_PRIVATE_KEY_PATH,
                    timeout=15,
                )
                return
            except Exception as e:
                last_err = e
                print(f"[runpod_pipeline] SSH connect attempt {i+1}/{attempts} failed: {e}")
                time.sleep(delay)
        raise RunPodPipelineError(f"Could not SSH into pod after {attempts} attempts: {last_err}")

    def _upload_images_to_volume(self) -> Iterator[Tuple[int, str]]:
        s3 = self._s3_client()
        prefix = f"{self.project_dir.name}/images/"
        images = list((self.project_dir / "images").glob("*"))
        total = len(images)

        def _upload_one(img_path):
            s3.upload_file(str(img_path), self.volume_id, prefix + img_path.name)
            return img_path.name

        completed = 0
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {executor.submit(_upload_one, p): p for p in images}
            for future in as_completed(futures):
                img_path = futures[future]
                try:
                    future.result()
                except Exception as e:
                    raise RunPodPipelineError(f"Failed to upload {img_path.name}: {e}")
                completed += 1
                pct = 1 + int((completed / total) * 2)
                yield pct, f"Uploading image {completed}/{total} to volume"

        used_bytes, obj_count = self._get_volume_usage()
        used_gb = used_bytes / (1024 ** 3)
        yield 3, f"Volume usage: {used_gb:.2f} GB across {obj_count} objects"

    def _run_remote_odm(self) -> Iterator[Tuple[int, str]]:
        print("[runpod_pipeline] _run_remote_odm() entered, building command")

        env_prefix = (
            "export PYTHONUNBUFFERED=1; "
            "export PATH=/code/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; "
            "export PYTHONPATH=:/code/SuperBuild/install/local/lib/python3.12/dist-packages:"
            "/code/SuperBuild/install/lib/python3.12/dist-packages:/code/SuperBuild/install/bin/opensfm; "
            "export LD_LIBRARY_PATH=:/code/SuperBuild/install/lib; "
            "export PDAL_DRIVER_PATH=/code/SuperBuild/install/bin; "
        )
        odm_cmd = (
            "/code/venv/bin/python3 /code/run.py "
            f"--project-path /workspace {self.project_dir.name} "
            f"--max-concurrency {self.max_concurrency} "
            "--orthophoto-resolution 3 "
            "--skip-3dmodel "
            "--dsm "
            + " ".join(self._quality_flags())
        )
        cmd = env_prefix + odm_cmd

        print(f"[runpod_pipeline] ODM command: {cmd}")

        self.ssh.get_transport().set_keepalive(30)

        # CRITICAL: get_pty=False prevents the EOF hang
        stdin, stdout, stderr = self.ssh.exec_command(cmd, get_pty=False)
        channel = stdout.channel
        channel.settimeout(1.0)

        last_progress = 25
        recent_lines = []
        last_output_time = time.time()
        STALL_TIMEOUT_S = 600

        print("[runpod_pipeline] Remote ODM process started")

        buf = ""
        while True:
            if channel.exit_status_ready() and not channel.recv_ready():
                break

            try:
                chunk = channel.recv(4096)
                if not chunk:
                    if channel.exit_status_ready():
                        break
                    continue

                last_output_time = time.time()
                buf += chunk.decode(errors="replace")

                # CRITICAL: Splitting on both \n and \r for real-time progress
                while "\n" in buf or "\r" in buf:
                    n_idx = buf.find("\n")
                    r_idx = buf.find("\r")

                    if n_idx != -1 and r_idx != -1:
                        split_idx = min(n_idx, r_idx)
                    else:
                        split_idx = max(n_idx, r_idx)

                    raw_line = buf[:split_idx]
                    buf = buf[split_idx + 1:]

                    line = raw_line.strip()
                    if not line:
                        continue

                    print(f"[RunPod ODM] {line}")

                    recent_lines.append(line)
                    if len(recent_lines) > 30:
                        recent_lines.pop(0)

                    for pattern, pct, label in STAGE_MARKERS:
                        if re.search(pattern, line, re.IGNORECASE):
                            scaled = 25 + int((pct / 97) * 63)
                            if scaled > last_progress:
                                last_progress = scaled
                                yield scaled, label
                            break
                    else:
                        if re.search(r"error|exception|traceback|failed|fatal", line, re.IGNORECASE):
                            yield last_progress, line

            except TimeoutError:
                pass
            except Exception as sock_err:
                if "timed out" not in str(sock_err).lower():
                    raise

            if time.time() - last_output_time > STALL_TIMEOUT_S:
                _, check_out, _ = self.ssh.exec_command(
                    f"test -f /workspace/{self.project_dir.name}/odm_orthophoto/odm_orthophoto.tif; echo $?"
                )
                if check_out.read().decode().strip() == "0":
                    print("[runpod_pipeline] SSH channel stalled but orthophoto already exists on remote — treating as success")
                    break
                raise RunPodPipelineError(
                    f"No output from remote ODM for {STALL_TIMEOUT_S}s (stuck at {last_progress}%). "
                    f"Last lines:\n" + "\n".join(recent_lines)
                )

        exit_status = channel.recv_exit_status() if channel.exit_status_ready() else 0
        print(f"[runpod_pipeline] Remote ODM exit status: {exit_status}")

        if exit_status not in (0,):
            error_output = stderr.read().decode(errors="replace").strip()
            error_context = "\n".join(recent_lines)
            raise RunPodPipelineError(
                f"ODM exited with code {exit_status}.\n"
                f"Last output:\n{error_context}\n"
                f"stderr:\n{error_output}"
            )

        _, check_out, _ = self.ssh.exec_command(
            f"test -f /workspace/{self.project_dir.name}/odm_orthophoto/odm_orthophoto.tif; echo $?"
        )
        result = check_out.read().decode().strip()
        if result != "0":
            raise RunPodPipelineError(
                "ODM exited successfully, but "
                "odm_orthophoto/odm_orthophoto.tif was not produced."
            )

    def _download_important_files(self):
        sftp = self.ssh.open_sftp()
        try:
            for rel_path in IMPORTANT_REMOTE_PATHS:
                remote_path = f"/workspace/{self.project_dir.name}/{rel_path}"
                local_path = self.project_dir / rel_path
                local_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    sftp.get(remote_path, str(local_path))
                except FileNotFoundError:
                    pass
        finally:
            sftp.close()