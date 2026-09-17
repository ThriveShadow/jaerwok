# runpod_client.py
import time
import requests
import shlex

API_BASE = "https://api.runpod.io/v2"


class RunPodError(RuntimeError):
    pass


class RunPodClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise RunPodError("RUNPOD_API_KEY is not set (env var or config.py)")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def get_ranked_cpu_flavors(self, min_vcpu: int = 4) -> list:
        """Returns a list of CPU flavor IDs ranked from cheapest to most expensive."""
        fallback_order = ["cpu3c", "cpu3g", "cpu5c", "cpu5g", "cpu3m"]
        try:
            res = requests.get(f"{API_BASE}/catalog/cpus", headers=self.headers, timeout=15)
            res.raise_for_status()
            flavors = res.json()

            if isinstance(flavors, dict):
                flavors = flavors.get("cpus", flavors.get("items", flavors.get("data", [])))

            candidates = [f for f in flavors if f.get("vcpuCount", 0) >= min_vcpu]
            if not candidates:
                candidates = flavors

            candidates.sort(key=lambda f: f.get("pricePerHr", f.get("price", 999)))
            if candidates:
                return [c["id"] for c in candidates]
        except Exception:
            # Already tolerant by design - falls back to a hardcoded order
            # on any error (network, parsing, missing keys, etc).
            pass
        return fallback_order

    def create_pod(self, name: str, image_name: str, cpu_flavor_id: str,
                   vcpu_count: int, container_disk_gb: int, env: dict,
                   docker_start_cmd: list, cloud_type: str = "COMMUNITY",
                   data_center_ids=None, network_volume_id: str = None) -> dict:

        # v2 replaces dockerEntrypoint and dockerStartCmd with a single `args` string.
        # We combine the old entrypoint and start cmd into it securely.
        args_str = shlex.join(["/bin/bash", "-c"] + docker_start_cmd)

        payload = {
            "name": name,
            "image": image_name,
            "cloud": cloud_type,
            "cpu": {
                "id": cpu_flavor_id,
                "vcpuCount": vcpu_count
            },
            "disk": container_disk_gb,
            "ports": ["22/tcp"],
            "env": env,
            "args": args_str,
        }

        if data_center_ids:
            # CreatePodRequest expects dataCenterIds as an array of strings
            payload["dataCenterIds"] = data_center_ids if isinstance(data_center_ids, list) else [data_center_ids]

        if network_volume_id:
            # Mounts network must be an array of objects
            payload["mounts"] = {
                "network": [
                    {
                        "volumeId": network_volume_id,
                        "path": "/workspace"
                    }
                ]
            }

        try:
            res = requests.post(f"{API_BASE}/pods", headers=self.headers, json=payload, timeout=30)
        except requests.exceptions.RequestException as e:
            raise RunPodError(f"Pod creation request failed: {e}") from e

        if res.status_code >= 300:
            raise RunPodError(f"Pod creation failed: {res.status_code} {res.text}")

        return res.json()

    def create_pod_with_fallback(self, name: str, image_name: str, min_vcpu: int,
                                 container_disk_gb: int, env: dict, docker_start_cmd: list,
                                 cloud_type: str = "COMMUNITY", data_center_ids=None,
                                 network_volume_id: str = None,
                                 retry_attempts: int = 10, retry_delay_s: int = 30):
        last_err = None

        for attempt in range(1, retry_attempts + 1):
            flavors = self.get_ranked_cpu_flavors(min_vcpu=min_vcpu)

            for flavor in flavors:
                yield f"Attempting to create pod with flavor: {flavor}..."
                try:
                    pod = self.create_pod(
                        name=name,
                        image_name=image_name,
                        cpu_flavor_id=flavor,
                        vcpu_count=min_vcpu,
                        container_disk_gb=container_disk_gb,
                        env=env,
                        docker_start_cmd=docker_start_cmd,
                        cloud_type=cloud_type,
                        data_center_ids=data_center_ids,
                        network_volume_id=network_volume_id,
                    )
                    yield f"Successfully created pod with {flavor}."
                    return pod
                except RunPodError as e:
                    last_err = e
                    if "no longer any instances available" in str(e).lower():
                        yield f"Flavor {flavor} is currently out of capacity. Trying next..."
                        continue
                    raise

            yield (
                f"All eligible flavors out of capacity in {cloud_type} "
                f"(attempt {attempt}/{retry_attempts}). Retrying in {retry_delay_s}s..."
            )
            time.sleep(retry_delay_s)

        raise RunPodError(
            f"Failed to create pod after {retry_attempts} attempts over "
            f"{retry_attempts * retry_delay_s}s. All eligible flavors for min_vcpu={min_vcpu} "
            f"remained out of capacity in {cloud_type}. Last error: {last_err}"
        )

    def get_pod(self, pod_id: str) -> dict:
        try:
            res = requests.get(f"{API_BASE}/pods/{pod_id}", headers=self.headers, timeout=30)
        except requests.exceptions.RequestException as e:
            raise RunPodError(f"Get pod request failed: {e}") from e

        if res.status_code >= 300:
            raise RunPodError(f"Get pod failed: {res.status_code} {res.text}")

        data = res.json()
        return data.get("pod", data)

    def terminate_pod(self, pod_id: str):
        try:
            res = requests.delete(f"{API_BASE}/pods/{pod_id}", headers=self.headers, timeout=30)
        except requests.exceptions.RequestException as e:
            raise RunPodError(f"Terminate pod request failed: {e}") from e

        if res.status_code >= 300 and res.status_code != 404:
            raise RunPodError(f"Terminate pod failed: {res.status_code} {res.text}")

    def create_network_volume(self, name: str, size_gb: int, data_center_id: str) -> dict:
        payload = {
            "name": name,
            "size": size_gb,
            "dataCenter": data_center_id,
        }
        try:
            res = requests.post(f"{API_BASE}/network-volumes", headers=self.headers, json=payload, timeout=30)
        except requests.exceptions.RequestException as e:
            raise RunPodError(f"Network volume creation request failed: {e}") from e

        if res.status_code >= 300:
            raise RunPodError(f"Network volume creation failed: {res.status_code} {res.text}")
        return res.json()

    def delete_network_volume(self, volume_id: str):
        try:
            res = requests.delete(f"{API_BASE}/network-volumes/{volume_id}", headers=self.headers, timeout=30)
        except requests.exceptions.RequestException as e:
            raise RunPodError(f"Delete network volume request failed: {e}") from e

        if res.status_code >= 300 and res.status_code != 404:
            raise RunPodError(f"Delete network volume failed: {res.status_code} {res.text}")

    def wait_for_ssh(self, pod_id: str, timeout_s: int = 300, poll_s: int = 5):
        start = time.time()
        while time.time() - start < timeout_s:
            try:
                pod = self.get_pod(pod_id)
            except RunPodError as e:
                print(f"[runpod_client] transient error polling pod {pod_id}, will retry: {e}")
                time.sleep(poll_s)
                continue

            runtime = pod.get("runtime") or {}
            for p in runtime.get("ports", []):
                if p.get("private") == 22 and p.get("public") and p.get("ip"):
                    return p["ip"], p["public"]
            time.sleep(poll_s)
        raise RunPodError("Timed out waiting for pod's SSH connection to become available")