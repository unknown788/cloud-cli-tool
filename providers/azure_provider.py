"""
providers/azure_provider.py

Concrete implementation of CloudProvider for Microsoft Azure.
Uses the Azure SDK (azure-mgmt-*) exclusively — no Azure CLI dependency.

Provisioning order (required by ARM dependency graph):
  Resource Group → VNet → Subnet → Public IP → NSG → NIC → VM
"""

import io
import os
import time
import socket
import paramiko
from pathlib import Path
from datetime import datetime

from azure.identity import DefaultAzureCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.core.exceptions import HttpResponseError

from .base import CloudProvider, ProvisionConfig, VMStatus

# Name of the Docker image built on the remote VM
DOCKER_IMAGE_NAME = "cloudlaunch-webapp"
DOCKER_CONTAINER_NAME = "cloudlaunch-container"
APP_DIR_NAME = "sample_app"
DOCKERFILE_NAME = "Dockerfile.app"


class AzureProvider(CloudProvider):
    """
    Azure implementation of CloudProvider.
    Authenticates via DefaultAzureCredential (env vars, managed identity, CLI login).

    Credential initialisation is lazy — the SDK clients are only created when
    a cloud operation is actually invoked. This allows plan/introspection
    operations to work without AZURE_SUBSCRIPTION_ID being set.
    """

    def __init__(self):
        # Lazy init — don't touch credentials until a cloud call is needed.
        # See _ensure_clients() below.
        self._resource_client = None
        self._network_client  = None
        self._compute_client  = None

    def _ensure_clients(self) -> None:
        """
        Initialise Azure SDK clients on first use.
        Raises EnvironmentError if AZURE_SUBSCRIPTION_ID is not set.
        Called at the top of every method that makes an Azure API call.
        """
        if self._resource_client is not None:
            return   # already initialised

        subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID")
        if not subscription_id:
            raise EnvironmentError(
                "AZURE_SUBSCRIPTION_ID environment variable is not set."
            )
        credential = DefaultAzureCredential()
        self._resource_client = ResourceManagementClient(credential, subscription_id)
        self._network_client  = NetworkManagementClient(credential, subscription_id)
        self._compute_client  = ComputeManagementClient(credential, subscription_id)

    # ------------------------------------------------------------------
    # provision
    # ------------------------------------------------------------------

    def provision(self, config: ProvisionConfig, log=print) -> dict:
        """
        Creates the full Azure resource stack for a single VM.

        Args:
            config: ProvisionConfig with all provisioning parameters.
            log:    Callable for progress output. Defaults to print.
                    The API layer passes a queue.put here for WebSocket streaming.

        Returns:
            State dict saved to state.json and used by deploy/destroy/status.
        """
        rg = config.resource_group
        vm = config.vm_name
        loc = config.location

        try:
            self._ensure_clients()
            # 1. Resource Group
            rg_result = self._resource_client.resource_groups.create_or_update(
                rg, {"location": loc}
            )
            log(f"✅ Resource Group '{rg_result.name}' ready.")

            # 2. Virtual Network
            vnet_result = self._network_client.virtual_networks.begin_create_or_update(
                rg,
                f"{vm}-vnet",
                {"location": loc, "address_space": {"address_prefixes": ["10.0.0.0/16"]}},
            ).result()
            log(f"✅ Virtual Network '{vnet_result.name}' ready.")

            # 3. Subnet
            subnet_result = self._network_client.subnets.begin_create_or_update(
                rg, vnet_result.name, "default", {"address_prefix": "10.0.0.0/24"}
            ).result()
            log(f"✅ Subnet 'default' ready.")

            # 4. Public IP
            ip_result = self._network_client.public_ip_addresses.begin_create_or_update(
                rg,
                f"{vm}-ip",
                {
                    "location": loc,
                    "public_ip_allocation_method": "Static",
                    "sku": {"name": "Standard"},
                },
            ).result()
            log(f"✅ Public IP '{ip_result.name}' → {ip_result.ip_address}")

            # 5. Network Security Group — allow SSH (22) and HTTP (80)
            nsg_result = self._network_client.network_security_groups.begin_create_or_update(
                rg,
                f"{vm}-nsg",
                {
                    "location": loc,
                    "security_rules": [
                        {
                            "name": "AllowSSH",
                            "protocol": "Tcp",
                            "direction": "Inbound",
                            "access": "Allow",
                            "source_address_prefix": "*",
                            "source_port_range": "*",
                            "destination_address_prefix": "*",
                            "destination_port_range": "22",
                            "priority": 1000,
                        },
                        {
                            "name": "AllowHTTP",
                            "protocol": "Tcp",
                            "direction": "Inbound",
                            "access": "Allow",
                            "source_address_prefix": "*",
                            "source_port_range": "*",
                            "destination_address_prefix": "*",
                            "destination_port_range": "80",
                            "priority": 1001,
                        },
                    ],
                },
            ).result()
            log(f"✅ NSG '{nsg_result.name}' ready (SSH + HTTP rules).")

            # 6. Network Interface Card
            nic_result = self._network_client.network_interfaces.begin_create_or_update(
                rg,
                f"{vm}-nic",
                {
                    "location": loc,
                    "ip_configurations": [
                        {
                            "name": "ipconfig1",
                            "subnet": {"id": subnet_result.id},
                            "public_ip_address": {"id": ip_result.id},
                        }
                    ],
                    "network_security_group": {"id": nsg_result.id},
                },
            ).result()
            log(f"✅ NIC '{nic_result.name}' ready.")

            # 7. Virtual Machine — generate a fresh SSH keypair in memory
            rsa_key = paramiko.RSAKey.generate(2048)
            pub_key_buf = io.StringIO()
            rsa_key.write_private_key(pub_key_buf)   # not used yet, just generate
            ssh_key_data = f"ssh-rsa {rsa_key.get_base64()} cloudlaunch-ephemeral"

            # Store private key as string so deploy() can use it without a file
            priv_key_buf = io.StringIO()
            rsa_key.write_private_key(priv_key_buf)
            ssh_private_key_str = priv_key_buf.getvalue()

            # cloud-init script: installs Docker during VM boot, in parallel
            # with our resource provisioning — so Docker is ready by the time
            # SSH connects, avoiding a separate install step (saves ~60-90s).
            import base64
            cloud_init = base64.b64encode(b"""#!/bin/bash
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker
""").decode()

            vm_params = {
                "location": loc,
                "hardware_profile": {
                    "vm_size": "Standard_B1s"
                },
                "storage_profile": {
                    "image_reference": {
                        "publisher": "Canonical",
                        "offer": "0001-com-ubuntu-server-jammy",
                        "sku": "22_04-lts-gen2",
                        "version": "latest",
                    }
                },
                "os_profile": {
                    "computer_name": vm,
                    "admin_username": config.admin_username,
                    "custom_data": cloud_init,
                    "linux_configuration": {
                        "disable_password_authentication": True,
                        "ssh": {
                            "public_keys": [
                                {
                                    "path": f"/home/{config.admin_username}/.ssh/authorized_keys",
                                    "key_data": ssh_key_data,
                                }
                            ]
                        },
                    },
                },
                "network_profile": {
                    "network_interfaces": [{"id": nic_result.id}]
                },
            }

            vm_result = self._compute_client.virtual_machines.begin_create_or_update(
                rg, vm, vm_params
            ).result()
            log(f"✅ Virtual Machine '{vm_result.name}' provisioned successfully.")

            state = {
                "provider": "azure",
                "resource_group": rg,
                "vm_name": vm,
                "location": loc,
                "admin_username": config.admin_username,
                "public_ip": ip_result.ip_address,
                # Private key stored as string — no file needed on the server
                "ssh_private_key_str": ssh_private_key_str,
            }
            log(f"🎉 Provisioning complete! VM reachable at {ip_result.ip_address}")
            return state

        except (HttpResponseError, FileNotFoundError) as ex:
            raise RuntimeError(f"Azure provisioning failed: {ex}") from ex

    # ------------------------------------------------------------------
    # deploy
    # ------------------------------------------------------------------

    def deploy(self, state: dict, log=print) -> None:
        """
        Deploys the containerised web app to the provisioned VM.

        Steps:
          1. Generate dynamic index.html from template
          2. SSH into VM
          3. Install Docker
          4. Upload Dockerfile + app directory via SFTP
          5. docker build → docker run (Nginx serving static HTML)
        """
        ip_address = state["public_ip"]
        username = state["admin_username"]
        # Load private key from state string (generated at provision time)
        # Fall back to file path for backward-compat with old state.json entries
        pkey = None
        if state.get("ssh_private_key_str"):
            pkey = paramiko.RSAKey.from_private_key(
                io.StringIO(state["ssh_private_key_str"])
            )
            private_key_path = None
        else:
            private_key_path = state.get(
                "ssh_private_key_path",
                str(Path.home() / ".ssh" / "id_rsa"),
            )

        # Generate dynamic HTML from template
        self._ensure_clients()
        log("⚙️  Generating dynamic deployment dashboard...")
        with open(f"{APP_DIR_NAME}/index.template.html", "r") as f:
            template = f.read()

        deploy_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        content = (
            template
            .replace("__IP_ADDRESS__", state["public_ip"])
            .replace("__VM_NAME__", state["vm_name"])
            .replace("__LOCATION__", state["location"])
            .replace("__DEPLOY_TIME__", deploy_time)
        )
        with open(f"{APP_DIR_NAME}/index.html", "w") as f:
            f.write(content)
        log("✅ Dashboard HTML generated.")

        try:
            # Wait until SSH daemon is ready (VM may still be booting)
            self._wait_for_ssh(ip_address, log=log)

            ssh = paramiko.SSHClient()
            # NOTE: In production, load known_hosts instead of AutoAddPolicy.
            # AutoAddPolicy is acceptable here because we just created this VM
            # and know its IP — it hasn't had time to be MITM'd.
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                ip_address, username=username,
                pkey=pkey, key_filename=private_key_path,
            )
            log(f"🔗 SSH connected to {username}@{ip_address}")

            # Docker is installed via cloud-init during VM boot.
            # Wait until the docker daemon is actually ready before proceeding.
            log("⏳ Waiting for Docker daemon (cloud-init)...")
            for _ in range(24):   # up to 2 minutes
                rc = self._exec(ssh, "sudo docker info > /dev/null 2>&1", log, check=False)
                if rc == 0:
                    break
                time.sleep(5)
            else:
                raise RuntimeError("Docker daemon did not start within 2 minutes.")
            log("✅ Docker ready.")

            # Upload app + Dockerfile via SFTP
            # Dockerfile.app is renamed to Dockerfile on the VM so docker build works
            sftp = ssh.open_sftp()
            remote_home = f"/home/{username}"
            sftp.put(DOCKERFILE_NAME, f"{remote_home}/Dockerfile")
            self._upload_directory(sftp, APP_DIR_NAME, f"{remote_home}/{APP_DIR_NAME}")
            sftp.close()
            log("✅ App files uploaded via SFTP.")

            # Build Docker image
            self._exec(ssh, f"cd {remote_home} && sudo docker build -t {DOCKER_IMAGE_NAME} .", log)

            # Stop old container (ignore error if not running)
            self._exec(ssh, f"sudo docker stop {DOCKER_CONTAINER_NAME} || true", log, check=False)
            self._exec(ssh, f"sudo docker rm {DOCKER_CONTAINER_NAME} || true", log, check=False)

            # Run new container — Nginx listens on 80 inside, mapped to host 80
            run_cmd = (
                f"sudo docker run -d "
                f"--name {DOCKER_CONTAINER_NAME} "
                f"--restart always "
                f"-p 80:80 "
                f"{DOCKER_IMAGE_NAME}"
            )
            self._exec(ssh, run_cmd, log)
            ssh.close()

            log(f"🎉 Deployment complete! View at: http://{ip_address}")

        except Exception as ex:
            raise RuntimeError(f"Deployment failed: {ex}") from ex

    # ------------------------------------------------------------------
    # destroy
    # ------------------------------------------------------------------

    def destroy(self, state: dict, log=print) -> None:
        """
        Deletes the entire Azure Resource Group and all resources within it.
        Blocks until deletion is confirmed by the Azure API.
        """
        rg = state["resource_group"]
        try:
            self._ensure_clients()
            log(f"🔥 Deleting resource group '{rg}'... (this takes 1-3 minutes)")
            poller = self._resource_client.resource_groups.begin_delete(rg)
            poller.result()  # Block until fully deleted
            log(f"✅ Resource group '{rg}' deleted.")
        except HttpResponseError as ex:
            raise RuntimeError(f"Azure destroy failed: {ex.message}") from ex

    # ------------------------------------------------------------------
    # get_status
    # ------------------------------------------------------------------

    def get_status(self, state: dict) -> VMStatus:
        """
        Queries Azure for the real-time power state of the VM.
        Returns a normalised VMStatus — never raw SDK objects.
        """
        rg = state["resource_group"]
        vm_name = state["vm_name"]

        try:
            self._ensure_clients()
            vm = self._compute_client.virtual_machines.get(
                rg, vm_name, expand="instanceView"
            )

            # Power state is in instance_view.statuses[1].code e.g. "PowerState/running"
            power_state = "unknown"
            if vm.instance_view and vm.instance_view.statuses:
                for s in vm.instance_view.statuses:
                    if s.code and s.code.startswith("PowerState/"):
                        power_state = s.code.split("/")[-1]
                        break

            os_disk = vm.storage_profile.os_disk
            disk_size = os_disk.disk_size_gb if os_disk else None

            return VMStatus(
                vm_name=vm.name,
                provider="azure",
                state=power_state,
                public_ip=state.get("public_ip"),
                location=vm.location,
                vm_size=vm.hardware_profile.vm_size,
                os_disk_size_gb=disk_size,
            )

        except HttpResponseError as ex:
            raise RuntimeError(f"Failed to get VM status: {ex.message}") from ex

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------

    def logs(self, state: dict, follow: bool = False, log=print) -> None:
        """
        SSH into the VM and stream Docker container logs.

        Uses an interactive channel (not exec_command) so that:
          - Output is streamed line-by-line as it arrives, not buffered.
          - Ctrl-C on the CLI side cleanly closes the channel.

        Args:
            state:  State dict from provision().
            follow: True  → `docker logs -f` (tail -f, streams forever).
                    False → `docker logs --tail 100` (last 100 lines, then exit).
            log:    Callable — same log= pattern used throughout the codebase.
        """
        ip_address = state["public_ip"]
        username = state["admin_username"]
        pkey = None
        if state.get("ssh_private_key_str"):
            pkey = paramiko.RSAKey.from_private_key(
                io.StringIO(state["ssh_private_key_str"])
            )
            private_key_path = None
        else:
            private_key_path = state.get(
                "ssh_private_key_path",
                str(Path.home() / ".ssh" / "id_rsa"),
            )

        flag = "-f" if follow else "--tail 200"
        cmd = f"sudo docker logs {flag} {DOCKER_CONTAINER_NAME} 2>&1"

        try:
            self._wait_for_ssh(ip_address, log=log)
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                ip_address, username=username,
                pkey=pkey, key_filename=private_key_path,
            )
            log(f"🔗 Connected to {username}@{ip_address}")

            if follow:
                log(f"📋 Streaming live logs (Ctrl-C to stop)...")
            else:
                log(f"📋 Last 200 lines of container logs:")

            # Use get_transport().open_session() for real-time streaming.
            # exec_command() reads all output after the command finishes —
            # useless for `docker logs -f`.
            transport = ssh.get_transport()
            channel = transport.open_session()
            channel.set_combine_stderr(True)   # merge stderr into stdout stream
            channel.exec_command(cmd)

            # Read line-by-line until the remote command exits or channel closes.
            buffer = b""
            try:
                while True:
                    # recv() blocks until data arrives or channel closes
                    chunk = channel.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    # Flush complete lines so the UI updates in real time
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        log(line.decode("utf-8", errors="replace"))
            except KeyboardInterrupt:
                # Ctrl-C: cleanly close the remote side before re-raising
                channel.close()
                log("\n⏹  Log stream stopped.")

            if buffer:
                log(buffer.decode("utf-8", errors="replace"))

            channel.close()
            ssh.close()

        except Exception as ex:
            raise RuntimeError(f"Failed to fetch logs: {ex}") from ex

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _wait_for_ssh(self, ip: str, port: int = 22, timeout: int = 180, log=print) -> None:
        """
        Poll TCP port 22 until the SSH daemon is ready or timeout expires.
        Azure VMs can take 60-120 s to fully boot after the API returns.
        """
        log(f"⏳ Waiting for SSH on {ip}:{port} (up to {timeout}s)...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((ip, port), timeout=5):
                    log(f"✅ SSH port open on {ip}.")
                    return
            except OSError:
                time.sleep(5)
        raise RuntimeError(
            f"Timed out waiting for SSH on {ip}:{port} after {timeout}s. "
            "The VM may still be booting — try deploying again in a minute."
        )

    def _exec(self, ssh: paramiko.SSHClient, command: str, log=print, check: bool = True) -> int:
        """Execute a remote command over SSH, stream stdout/stderr to log().

        Args:
            check: If True (default), raise RuntimeError on non-zero exit.
                   Pass check=False for commands where failure is acceptable
                   (e.g. `docker stop ... || true`).
        """
        log(f"  $ {command}")
        _, stdout, stderr = ssh.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        output = stdout.read().decode().strip()
        error  = stderr.read().decode().strip()
        if output:
            log(f"    {output}")
        if exit_status != 0:
            if error:
                log(f"    ⚠ {error}")
            if check:
                raise RuntimeError(
                    f"Command failed (exit {exit_status}): {command}\n{error}"
                )
        return exit_status

    def _upload_directory(self, sftp: paramiko.SFTPClient, local_path: str, remote_path: str) -> None:
        """Recursively upload a local directory to the remote VM via SFTP."""
        import os
        try:
            sftp.stat(remote_path)
        except FileNotFoundError:
            sftp.mkdir(remote_path)

        for item in os.listdir(local_path):
            local_item = os.path.join(local_path, item)
            remote_item = f"{remote_path}/{item}"
            if os.path.isfile(local_item):
                sftp.put(local_item, remote_item)
            elif os.path.isdir(local_item):
                self._upload_directory(sftp, local_item, remote_item)
