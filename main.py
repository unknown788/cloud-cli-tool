import os
import json
import subprocess
from typing import Optional

import typer
from dotenv import load_dotenv
from pathlib import Path

from providers import get_provider, ProvisionConfig
from cli.display import (
    print_banner,
    print_plan_table,
    print_status_panel,
    make_log_handler,
    print_success,
    print_error,
)

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
    provider:       str  = typer.Option("azure",                                    help="Cloud provider: azure | aws"),
    resource_group: str  = typer.Option("cloudlaunch-rg",                           help="Resource group / project name."),
    location:       str  = typer.Option("southeastasia",                            help="Cloud region."),
    vm_name:        str  = typer.Option("cloudlaunch-vm",                           help="Name for the virtual machine."),
    admin_username: str  = typer.Option("azureuser",                                help="Admin username on the VM."),
    ssh_key_path:   str  = typer.Option(str(Path.home() / ".ssh" / "id_rsa.pub"),   help="Path to SSH public key."),
    plan:           bool = typer.Option(False, "--plan",                             help="Preview what will be created without making any changes."),
):
    """Provision a complete VM stack on the selected cloud provider."""
    print_banner()

    config = ProvisionConfig(
        vm_name=vm_name,
        location=location,
        admin_username=admin_username,
        ssh_key_path=ssh_key_path,
        resource_group=resource_group,
    )

    try:
        p = get_provider(provider)

        if plan:
            # --plan mode: show what WOULD be created, then exit. Zero cloud calls.
            resources = p.get_plan(config)
            print_plan_table(
                resources,
                config_summary={
                    "provider": provider,
                    "location": location,
                    "vm_size": "Standard_B1s",
                },
            )
            raise typer.Exit(code=0)

        # Normal mode: actually provision
        log = make_log_handler()
        state = p.provision(config, log=log)
        save_state(state)
        print_success(f"Provisioning complete! State saved to '{STATE_FILE}'.\nNext: run  python main.py deploy")

    except typer.Exit:
        raise
    except (RuntimeError, EnvironmentError, ValueError) as e:
        print_error(str(e))
        raise typer.Exit(code=1)


@app.command()
def deploy(
    provider: str = typer.Option("azure", help="Cloud provider: azure | aws"),
):
    """Build and run the containerised web app on the provisioned VM."""
    state = load_state()
    if not state:
        print_error("No state found. Run 'provision' first.")
        raise typer.Exit(code=1)

    print_banner()
    try:
        p = get_provider(provider)
        p.deploy(state, log=make_log_handler())
    except (RuntimeError, EnvironmentError, ValueError) as e:
        print_error(str(e))
        raise typer.Exit(code=1)


@app.command()
def destroy(
    provider: str = typer.Option("azure", help="Cloud provider: azure | aws"),
):
    """Tear down all cloud resources and delete local state."""
    state = load_state()
    if not state:
        print_error("No state found. Nothing to destroy.")
        raise typer.Exit()

    rg = state.get("resource_group", state.get("vm_name", "unknown"))
    typer.echo("")
    typer.secho(
        f"  WARNING: This will permanently delete '{rg}' and ALL its resources.",
        fg=typer.colors.RED, bold=True,
    )
    if not typer.confirm("  Are you absolutely sure?"):
        raise typer.Abort()

    try:
        p = get_provider(provider)
        p.destroy(state, log=make_log_handler())
        os.remove(STATE_FILE)
        print_success(f"All resources in '{rg}' deleted. State file removed.")
    except (RuntimeError, EnvironmentError, ValueError) as e:
        print_error(str(e))
        raise typer.Exit(code=1)


@app.command()
def status(
    provider: str = typer.Option("azure", help="Cloud provider: azure | aws"),
):
    """Query the cloud API for the real-time status of your VM."""
    state = load_state()
    if not state:
        print_error("No state found. Run 'provision' first.")
        raise typer.Exit(code=1)

    try:
        p = get_provider(provider)
        vm_status = p.get_status(state)
        print_status_panel(vm_status)
    except (RuntimeError, EnvironmentError, ValueError) as e:
        print_error(str(e))
        raise typer.Exit(code=1)


@app.command()
def logs(
    provider: str  = typer.Option("azure",  help="Cloud provider: azure | aws"),
    follow:   bool = typer.Option(False, "--follow", "-f", help="Stream logs continuously (tail -f style)."),
):
    """Stream Docker container logs from the running VM."""
    state = load_state()
    if not state:
        print_error("No state found. Run 'provision' then 'deploy' first.")
        raise typer.Exit(code=1)

    print_banner()
    try:
        p = get_provider(provider)
        p.logs(state, follow=follow, log=make_log_handler())
    except KeyboardInterrupt:
        # Ctrl-C during `--follow` is normal user behaviour — not an error.
        pass
    except (RuntimeError, EnvironmentError, ValueError) as e:
        print_error(str(e))
        raise typer.Exit(code=1)


@app.command()
def ssh(
    provider: str = typer.Option("azure", help="Cloud provider: azure | aws"),
):
    """Open an interactive SSH session directly to the VM."""
    state = load_state()
    if not state:
        print_error("No state found. Run 'provision' first.")
        raise typer.Exit(code=1)

    ip       = state.get("public_ip")
    username = state.get("admin_username", "azureuser")

    if not ip:
        print_error("No public IP in state. Reprovisioning may be required.")
        raise typer.Exit(code=1)

    key_path = str(Path.home() / ".ssh" / "id_rsa")
    print_banner()
    typer.secho(f"\n  Connecting → {username}@{ip}", fg=typer.colors.CYAN, bold=True)
    typer.secho(f"  Key: {key_path}", fg=typer.colors.BRIGHT_BLACK)
    typer.echo("")

    # os.execvp() *replaces* this process with SSH — stdin/stdout/stderr are
    # all inherited so the terminal is fully interactive. No subprocess overhead.
    ssh_args = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-i", key_path,
        f"{username}@{ip}",
    ]
    os.execvp("ssh", ssh_args)


@app.command()
def redeploy(
    provider: str = typer.Option("azure", help="Cloud provider: azure | aws"),
):
    """Re-deploy the app to the existing VM without re-provisioning.

    Use this after changing your application code. Skips the expensive
    VM-creation steps and goes straight to Docker build + run.
    """
    state = load_state()
    if not state:
        print_error("No state found. Run 'provision' first.")
        raise typer.Exit(code=1)

    print_banner()
    ip = state.get("public_ip", "unknown")
    typer.secho(f"\n  Re-deploying to existing VM at {ip}...\n", fg=typer.colors.CYAN)

    try:
        p = get_provider(provider)
        p.deploy(state, log=make_log_handler())
        print_success(
            f"Re-deployment complete!\n"
            f"  ➜  http://{ip}"
        )
    except (RuntimeError, EnvironmentError, ValueError) as e:
        print_error(str(e))
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
