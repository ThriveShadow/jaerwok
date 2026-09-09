#runpod_client.py
import time
import requests

REST_BASE = "https://rest.runpod.io/v1"
CATALOG_BASE = "https://api.runpod.io/v2"


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
            res = requests.get(f"{CATALOG_BASE}/catalog/cpus", headers=self.headers, timeout=15)
            res.raise_for_status()
            flavors = res.json()
            
            if isinstance(flavors, dict):
                flavors = flavors.get("items", flavors.get("data", []))
                
            candidates = [f for f in flavors if f.get("vcpuCount", 0) >= min_vcpu]
            if not candidates:
                candidates = flavors
                
            candidates.sort(key=lambda f: f.get("pricePerHr", f.get("price", 999)))
            if candidates:
                return [c["id"] for c in candidates]
        except Exception:
            pass
        return fallback_order

    def create_pod(self, name: str, image_name: str, cpu_flavor_id: str,
                   vcpu_count: int, container_disk_gb: int, env: dict,
                   docker_start_cmd: list, cloud_type: str = "COMMUNITY",
                   data_center_ids=None, network_volume_id: str = None) -> dict:
        payload = {
            "name": name,
            "imageName": image_name,
            "cloudType": cloud_type,
            "computeType": "CPU",
            "cpuFlavorIds": [cpu_flavor_id],
            "vcpuCount": vcpu_count,
            "containerDiskInGb": container_disk_gb,
            "ports": ["22/tcp"],
            "supportPublicIp": True,
            "env": env,
            "dockerEntrypoint": ["/bin/bash", "-c"],
            "dockerStartCmd": docker_start_cmd,
        }
        if data_center_ids:
            payload["dataCenterIds"] = data_center_ids
        if network_volume_id:
            payload["networkVolumeId"] = network_volume_id

        res = requests.post(f"{REST_BASE}/pods", headers=self.headers, json=payload, timeout=30)
        if res.status_code >= 300:
            raise RunPodError(f"Pod creation failed: {res.status_code} {res.text}")
        return res.json()

    def create_pod_with_fallback(self, name: str, image_name: str, min_vcpu: int,
                                 container_disk_gb: int, env: dict, docker_start_cmd: list,
                                 cloud_type: str = "COMMUNITY", data_center_ids=None,
                                 network_volume_id: str = None,
                                 retry_attempts: int = 10, retry_delay_s: int = 30):
        """Generator that yields human-readable status strings while attempting
        to create a pod, retrying across flavors and across full passes.
        Returns the created pod dict (accessible via the generator's return
        value / StopIteration.value) on success, or raises RunPodError if
        every attempt is exhausted."""
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
                    if "no longer any instances available" in str(e):
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
        res = requests.get(f"{REST_BASE}/pods/{pod_id}", headers=self.headers, timeout=15)
        if res.status_code >= 300:
            raise RunPodError(f"Get pod failed: {res.status_code} {res.text}")
        return res.json()

    def terminate_pod(self, pod_id: str):
        res = requests.delete(f"{REST_BASE}/pods/{pod_id}", headers=self.headers, timeout=30)
        if res.status_code >= 300 and res.status_code != 404:
            raise RunPodError(f"Terminate pod failed: {res.status_code} {res.text}")

    def create_network_volume(self, name: str, size_gb: int, data_center_id: str) -> dict:
        payload = {
            "name": name,
            "size": size_gb,
            "dataCenterId": data_center_id,
        }
        res = requests.post(f"{REST_BASE}/networkvolumes", headers=self.headers, json=payload, timeout=30)
        if res.status_code >= 300:
            raise RunPodError(f"Network volume creation failed: {res.status_code} {res.text}")
        return res.json()

    def delete_network_volume(self, volume_id: str):
        res = requests.delete(f"{REST_BASE}/networkvolumes/{volume_id}", headers=self.headers, timeout=30)
        if res.status_code >= 300 and res.status_code != 404:
            raise RunPodError(f"Delete network volume failed: {res.status_code} {res.text}")
            
    def wait_for_ssh(self, pod_id: str, timeout_s: int = 300, poll_s: int = 5):
        start = time.time()
        while time.time() - start < timeout_s:
            pod = self.get_pod(pod_id)
            ip = pod.get("publicIp") or pod.get("ip")
            port_mappings = pod.get("portMappings") or {}
            ssh_port = port_mappings.get("22")
            if ip and ssh_port:
                return ip, ssh_port
            time.sleep(poll_s)
        raise RunPodError("Timed out waiting for pod's SSH connection to become available")