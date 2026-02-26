import os
import json
from typing import Optional

import typer
from dotenv import load_dotenv
from pathlib import Path

from providers import get_provider, ProvisionConfig

load_dotenv()

STATE_FILE = "state.json"
app = typer.Typer(
    name="cloudlaunch",
    help="CloudLaunch - provision, deploy, and manage cloud VMs.",
    add_completion=False,
)


def save_state(data: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=4)


def load_state() -> Optional[dict]:
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return json.load(f)


@app.command()
def provision(
    provider:       str = typer.Option("azure",                                   help="Cloud provider: azure | aws"),
    resource_group: str = typer.Option("cloudlaunch-rg",                          help="Resource group / project name."),
    location:       str = typer.Option("southeastasia",                           help="Cloud region."),
    vm_name:        str = typer.Option("cloudlaunch-vm",                          help="Name for the virtual machine."),
    admin_username: str = typer.Option("azureuser",                               help="Admin username on the VM."),
    ssh_key_path:   str = typer.Option(str(Path.home() / ".ssh" / "id_rsa.pub"), help="Path to SSH public key."),
):
    """Provision a complete VM stack on the selected cloud provider."""
    typer.echo("")
    typer.secho(f"CloudLaunch - Provisioning on {provider.upper()}", fg=typer.colors.CYAN, bold=True)
    config = ProvisionConfig(
        vm_name=vm_name, location=location,
        admin_username=admin_username, ssh_key_path=ssh_key_path,
        resource_group=resource_group,
    )
    try:
        p = get_provider(provider)
        state = p.provision(config, log=typer.echo)
        save_state(state)
        typer.echo("")
        typer.secho(f"[OK] State saved to {STATE_FILE!r}", fg=typer.colors.GREEN)
        typer.secho("Next step: run python main.py deploy", bold=True)
    except (RuntimeError, EnvironmentError, ValueError) as e:
        typer.echo("")
        typer.secho(f"[ERROR] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def deploy(
    provider: str = typer.Option("azure", help="Cloud provider: azure | aws"),
):
    """Build and run the containerised web app on the provisioned VM."""
    state = load_state()
    if not state:
        typer.secho("[ERROR] No state found. Run provision first.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho(f"CloudLaunch - Deploying to {state['vm_name']}...", fg=typer.colors.CYAN, bold=True)
    try:
        p = get_provider(provider)
        p.deploy(state, log=typer.echo)
    except (RuntimeError, EnvironmentError, ValueError) as e:
        typer.secho(f"[ERROR] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def destroy(
    provider: str = typer.Option("azure", help="Cloud provider: azure | aws"),
):
    """Tear down all cloud resources and delete local state."""
    state = load_state()
    if not state:
        typer.secho("No state found. Nothing to destroy.", fg=typer.colors.YELLOW)
        raise typer.Exit()
    rg = state.get("resource_group", state.get("vm_name", "unknown"))
    typer.secho(f"WARNING: This will permanently delete {rg!r} and ALL its resources.", fg=typer.colors.RED, bold=True)
    if not typer.confirm("Are you absolutely sure?"):
        raise typer.Abort()
    try:
        p = get_provider(provider)
        p.destroy(state, log=typer.echo)
        os.remove(STATE_FILE)
        typer.echo("")
        typer.secho("[OK] All resources deleted. State file removed.", fg=typer.colors.GREEN)
    except (RuntimeError, EnvironmentError, ValueError) as e:
        typer.secho(f"[ERROR] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def status(
    provider: str = typer.Option("azure", help="Cloud provider: azure | aws"),
):
    """Query the cloud API for the real-time status of your VM."""
    state = load_state()
    if not state:
        typer.secho("[ERROR] No state found. Run provision first.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    try:
        p = get_provider(provider)
        vm_status = p.get_status(state)
        color = typer.colors.GREEN if vm_status.state == "running" else typer.colors.YELLOW
        typer.secho(f"VM Status: {vm_status.vm_name}", bold=True)
        typer.echo(f"  Provider  : {vm_status.provider.upper()}")
        typer.echo(f"  State     : ", nl=False)
        typer.secho(vm_status.state.upper(), fg=color, bold=True)
        typer.echo(f"  Public IP : {vm_status.public_ip}")
        typer.echo(f"  Location  : {vm_status.location}")
        typer.echo(f"  VM Size   : {vm_status.vm_size}")
        if vm_status.os_disk_size_gb:
            typer.echo(f"  Disk      : {vm_status.os_disk_size_gb} GB")
        if vm_status.state == "running":
            typer.secho(f"  http://{vm_status.public_ip}", fg=typer.colors.CYAN, bold=True)
    except (RuntimeError, EnvironmentError, ValueError) as e:
        typer.secho(f"[ERROR] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
