"""
providers/base.py

Defines the abstract CloudProvider interface.
Every cloud provider (Azure, AWS, GCP) must implement this contract.
This is the Strategy Pattern — the CLI doesn't care which provider
is underneath, it just calls the same methods.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict


@dataclass
class ProvisionConfig:
    """
    All inputs needed to provision a VM on any cloud.
    Provider-specific details (like Azure resource group) live
    in the concrete provider, not here.
    """
    vm_name: str
    location: str
    admin_username: str
    ssh_key_path: str
    resource_group: str  # Azure: resource group | AWS: used as tag | GCP: project


@dataclass
class VMStatus:
    """
    Normalised VM status returned by any provider's get_status().
    The API and CLI always get the same shape regardless of cloud.
    """
    vm_name: str
    provider: str
    state: str           # "running" | "stopped" | "deallocated" | "unknown"
    public_ip: Optional[str]
    location: str
    vm_size: str
    os_disk_size_gb: Optional[int]


class CloudProvider(ABC):
    """
    Abstract base class for all cloud providers.

    To add a new provider (e.g. GCP):
      1. Create providers/gcp_provider.py
      2. Subclass CloudProvider
      3. Implement all abstract methods
      4. Register it in providers/__init__.py get_provider()

    That's it. The CLI, API, and WebSocket layers need zero changes.
    """

    @abstractmethod
    def provision(self, config: ProvisionConfig) -> dict:
        """
        Create all infrastructure required to run a VM.
        Must return a state dict with at minimum:
          { "vm_name", "public_ip", "admin_username", "location", "resource_group" }
        """
        ...

    @abstractmethod
    def deploy(self, state: dict) -> None:
        """
        Install Docker on the VM, upload the app, build and run the container.
        Receives the state dict returned by provision().
        """
        ...

    @abstractmethod
    def destroy(self, state: dict) -> None:
        """
        Tear down all infrastructure created by provision().
        Must be idempotent — safe to call even if partially provisioned.
        """
        ...

    @abstractmethod
    def get_status(self, state: dict) -> VMStatus:
        """
        Query the cloud API for the real-time status of the VM.
        Returns a normalised VMStatus — never raw SDK objects.
        """
        ...

    @abstractmethod
    def logs(self, state: dict, follow: bool = False, log=print) -> None:
        """
        Stream Docker container logs from the VM.

        Args:
            state:  State dict from provision().
            follow: If True, tail -f style (stream until Ctrl-C).
                    If False, dump the last N lines and return.
            log:    Callable for output. Same pattern as provision/deploy.
        """
        ...

    def get_plan(self, config: ProvisionConfig) -> List[Dict]:
        """
        Returns a list of resources that WOULD be created by provision().
        Default implementation — providers can override for accuracy.
        Each item: { "resource", "name", "type", "detail" }
        """
        return [
            {"resource": "Resource Group",    "name": config.resource_group,             "type": "container",  "detail": config.location},
            {"resource": "Virtual Network",   "name": f"{config.vm_name}-vnet",          "type": "network",    "detail": "10.0.0.0/16"},
            {"resource": "Subnet",            "name": "default",                         "type": "network",    "detail": "10.0.0.0/24"},
            {"resource": "Public IP",         "name": f"{config.vm_name}-ip",            "type": "network",    "detail": "Static / Standard SKU"},
            {"resource": "Network Sec Group", "name": f"{config.vm_name}-nsg",           "type": "security",   "detail": "Allow SSH:22, HTTP:80"},
            {"resource": "Network Interface", "name": f"{config.vm_name}-nic",           "type": "network",    "detail": "Attached to vnet + NSG"},
            {"resource": "Virtual Machine",   "name": config.vm_name,                    "type": "compute",    "detail": "Standard_B1s · Ubuntu 22.04 LTS"},
        ]
