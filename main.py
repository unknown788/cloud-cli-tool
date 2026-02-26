import os
import json
import paramiko
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime

import typer
from azure.identity import DefaultAzureCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.compute import ComputeManagementClient
from azure.core.exceptions import HttpResponseError


STATE_FILE = "state.json"
APP_DIR_NAME = "sample_app"
DOCKERFILE_NAME = "Dockerfile"

app = typer.Typer()
load_dotenv()


def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=4)


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def upload_directory(sftp, local_path, remote_path):
    if not os.path.isdir(local_path):
        raise FileNotFoundError(f"Local directory not found: {local_path}")
    try:
        sftp.stat(remote_path)
    except FileNotFoundError:
        sftp.mkdir(remote_path)
    for item in os.listdir(local_path):
        local_item_path = os.path.join(local_path, item)
        remote_item_path = f"{remote_path}/{item}"
        if os.path.isfile(local_item_path):
            sftp.put(local_item_path, remote_item_path)
        elif os.path.isdir(local_item_path):
            sftp.mkdir(remote_item_path)
            upload_directory(sftp, local_item_path, remote_item_path)


def exec_remote_command(ssh, command):
    typer.echo(f"Executing: {command}")
    stdin, stdout, stderr = ssh.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    output = stdout.read().decode()
    error = stderr.read().decode()
    if exit_status != 0:
        typer.secho(f"Error executing command: {command}", fg=typer.colors.RED)
        if error:
            typer.secho(error, fg=typer.colors.RED)
    else:
        if output:
            typer.secho(output, fg=typer.colors.GREEN)
    return exit_status


@app.command()
def provision(
    resource_group: str = typer.Option(
        "vm-project-rg", help="The name of the resource group."),
    location: str = typer.Option(
        "southeastasia", help="The Azure region for all resources."),
    vm_name: str = typer.Option(
        "my-app-vm", help="The name for the new virtual machine."),
    admin_username: str = typer.Option(
        "azureuser", help="The admin username for the VM."),
    ssh_key_path: str = typer.Option(
        f"{Path.home()}/.ssh/id_rsa.pub", help="Path to your SSH public key."),
):
    """Provisions a complete Virtual Machine on Azure."""
    credential = DefaultAzureCredential()
    subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
    resource_client = ResourceManagementClient(credential, subscription_id)
    network_client = NetworkManagementClient(credential, subscription_id)
    compute_client = ComputeManagementClient(credential, subscription_id)
    typer.secho(
        f"🚀 Starting provisioning for VM '{vm_name}'...", fg=typer.colors.CYAN)
    try:
        rg_result = resource_client.resource_groups.create_or_update(
            resource_group, {"location": location})
        typer.secho(f"✅ Provisioned resource group '{rg_result.name}'.")
        vnet_result = network_client.virtual_networks.begin_create_or_update(
            resource_group, f"{vm_name}-vnet", {"location": location, "address_space": {"address_prefixes": ["10.0.0.0/16"]}}).result()
        typer.secho(f"✅ Provisioned virtual network '{vnet_result.name}'.")
        subnet_result = network_client.subnets.begin_create_or_update(
            resource_group, vnet_result.name, "default", {"address_prefix": "10.0.0.0/24"}).result()
        typer.secho(f"✅ Provisioned subnet 'default'.")
        ip_params = {"location": location, "public_ip_allocation_method": "Static", "sku": {
            "name": "Standard"}}
        ip_result = network_client.public_ip_addresses.begin_create_or_update(
            resource_group, f"{vm_name}-ip", ip_params).result()
        typer.secho(f"✅ Provisioned public IP address '{ip_result.name}'.")

        nsg_params = {
            "location": location,
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
        }
        nsg_result = network_client.network_security_groups.begin_create_or_update(
            resource_group, f"{vm_name}-nsg", nsg_params).result()
        typer.secho(
            f"✅ Provisioned NSG '{nsg_result.name}' with SSH and HTTP rules.")

        nic_params = {
            "location": location,
            "ip_configurations": [
                {
                    "name": "ipconfig1",
                    "subnet": {"id": subnet_result.id},
                    "public_ip_address": {"id": ip_result.id},
                }
            ],
            "network_security_group": {"id": nsg_result.id},
        }
        nic_result = network_client.network_interfaces.begin_create_or_update(
            resource_group, f"{vm_name}-nic", nic_params).result()
        typer.secho(f"✅ Provisioned network interface '{nic_result.name}'.")
        with open(ssh_key_path, "r") as f:
            ssh_key = f.read()

        vm_params = {
            "location": location,
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
                "computer_name": vm_name,
                "admin_username": admin_username,
                "linux_configuration": {
                    "disable_password_authentication": True,
                    "ssh": {
                        "public_keys": [
                            {
                                "path": f"/home/{admin_username}/.ssh/authorized_keys",
                                "key_data": ssh_key,
                            }
                        ]
                    },
                },
            },
            "network_profile": {
                "network_interfaces": [{"id": nic_result.id}]
            },
        }
        vm_result = compute_client.virtual_machines.begin_create_or_update(
            resource_group, vm_name, vm_params).result()
        typer.secho(
            f"✅ Successfully provisioned virtual machine '{vm_result.name}'.")
        state_data = {"resource_group": resource_group, "vm_name": vm_name, "location": location,
                      "admin_username": admin_username, "public_ip": ip_result.ip_address}
        save_state(state_data)
        typer.secho(f"✅ State saved to {STATE_FILE}", fg=typer.colors.GREEN)
        typer.secho("\n🎉 Provisioning complete! 🎉", bold=True)
        typer.secho(
            f"Connect to your VM using: ssh {admin_username}@{ip_result.ip_address}", bold=True)
    except (HttpResponseError, FileNotFoundError) as ex:
        typer.secho(f"❌ Error during provisioning: {ex}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def deploy():
    """Generates a dynamic dashboard and deploys it to the VM using Docker."""
    state = load_state()
    if not state:
        typer.secho("❌ No state file found. Run 'provision' first.",
                    fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # --- 1. GENERATE DYNAMIC HTML ---
    typer.secho("⚙️ Generating dynamic deployment dashboard...",
                fg=typer.colors.YELLOW)
    with open(f"{APP_DIR_NAME}/index.template.html", "r") as f:
        template = f.read()

    # Get current time and format it
    deploy_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

    # Replace placeholders with real data from state
    content = template.replace("__IP_ADDRESS__", state["public_ip"])
    content = content.replace("__VM_NAME__", state["vm_name"])
    content = content.replace("__LOCATION__", state["location"])
    content = content.replace("__DEPLOY_TIME__", deploy_time)

    # Write the final HTML file that will be deployed
    with open(f"{APP_DIR_NAME}/index.html", "w") as f:
        f.write(content)
    typer.secho("✅ Dashboard generated successfully.", fg=typer.colors.GREEN)

    # --- 2. DEPLOY TO VM ---
    ip_address = state["public_ip"]
    username = state["admin_username"]
    typer.secho(
        f"🚀 Starting Docker deployment to {username}@{ip_address}...", fg=typer.colors.CYAN)

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        private_key_path = f"{Path.home()}/.ssh/id_rsa"
        ssh.connect(ip_address, username=username,
                    key_filename=private_key_path)

        # Install Docker
        exec_remote_command(ssh, "sudo apt-get update -y")
        exec_remote_command(ssh, "sudo apt-get install -y docker.io")
        exec_remote_command(ssh, f"sudo usermod -aG docker {username}")
        typer.secho("✅ Docker installed on the VM.", fg=typer.colors.GREEN)

        # Upload application code and Dockerfile
        sftp = ssh.open_sftp()
        remote_home = f"/home/{username}"
        sftp.put(DOCKERFILE_NAME, f"{remote_home}/{DOCKERFILE_NAME}")
        upload_directory(sftp, APP_DIR_NAME, f"{remote_home}/{APP_DIR_NAME}")
        sftp.close()
        typer.secho("✅ Application code and Dockerfile uploaded.",
                    fg=typer.colors.GREEN)

        # Build the Docker image on the VM
        exec_remote_command(
            ssh, f"cd {remote_home} && sudo docker build -t my-webapp-app .")

        # Run the Docker container
        typer.secho("Stopping any old containers...")
        exec_remote_command(
            ssh, "sudo docker stop my-webapp-container || true")
        exec_remote_command(ssh, "sudo docker rm my-webapp-container || true")

        typer.secho("Starting new container...")
        # Note the port mapping is 80:80 because Nginx in the container listens on port 80
        run_command = "sudo docker run -d --name my-webapp-container --restart always -p 80:80 my-webapp-app"
        exec_remote_command(ssh, run_command)

        ssh.close()
        typer.secho("\n🎉 Docker deployment complete! 🎉", bold=True)
        typer.secho(
            f"View your application dashboard at: http://{ip_address}", bold=True)

    except Exception as ex:
        typer.secho(f"❌ Error during deployment: {ex}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

# --- DESTROY COMMAND (No changes) ---


@app.command()
def destroy():
    """Destroys all Azure resources created by the 'provision' command."""
    state = load_state()
    if not state:
        typer.secho("🤷 No state file found. Nothing to destroy.",
                    fg=typer.colors.YELLOW)
        raise typer.Exit()

    resource_group = state["resource_group"]
    typer.secho(
        f"🔥 This will delete the entire resource group '{resource_group}' and ALL its resources.",
        fg=typer.colors.RED,
        bold=True,
    )
    if not typer.confirm("Are you sure you want to proceed?"):
        raise typer.Abort()

    try:
        credential = DefaultAzureCredential()
        subscription_id = os.environ["AZURE_SUBSCRIPTION_ID"]
        resource_client = ResourceManagementClient(credential, subscription_id)

        typer.echo("🚀 Starting destruction via Azure SDK...")
        poller = resource_client.resource_groups.begin_delete(resource_group)
        typer.secho(
            f"⏳ Deletion of resource group '{resource_group}' is in progress...",
            fg=typer.colors.YELLOW,
        )
        poller.result()  # Block until deletion is fully complete

        os.remove(STATE_FILE)
        typer.secho(
            f"✅ Resource group '{resource_group}' has been fully deleted.",
            fg=typer.colors.GREEN,
        )
        typer.secho(
            f"✅ Local state file '{STATE_FILE}' has been removed.",
            fg=typer.colors.GREEN,
        )
    except HttpResponseError as e:
        typer.secho(f"❌ Azure error during destruction: {e.message}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    except KeyError:
        typer.secho("❌ AZURE_SUBSCRIPTION_ID environment variable not set.", fg=typer.colors.RED)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
