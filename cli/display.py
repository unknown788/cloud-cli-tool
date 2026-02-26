"""
cli/display.py

All Rich-based terminal rendering for CloudLaunch.
Centralising this here means:
  - main.py never imports Rich directly
  - The API layer (Phase 4) can skip this entirely
  - Tests can mock this module without touching provider logic

Functions:
  print_banner()          — CloudLaunch header
  print_plan_table()      — Plan preview table
  print_status_panel()    — VM status panel
  make_log_handler()      — Returns a log callable with Rich formatting
  print_success()         — Styled success message
  print_error()           — Styled error message
"""

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich import box
from typing import List, Dict, Callable

console = Console()

# Azure VM size → approximate hourly USD cost (illustrative, not contractual)
_COST_MAP = {
    "Standard_B1s":  0.0104,
    "Standard_B2s":  0.0416,
    "Standard_B1ms": 0.0207,
    "Standard_D2s_v3": 0.096,
}
_DEFAULT_COST = 0.02


def print_banner() -> None:
    """Print the CloudLaunch ASCII banner."""
    banner = Text()
    banner.append("  ☁  CloudLaunch", style="bold cyan")
    banner.append("  |  ", style="dim")
    banner.append("Cloud VM Provisioning CLI", style="italic white")
    console.print(Panel(banner, border_style="cyan", padding=(0, 2)))


def print_plan_table(resources: List[Dict], config_summary: Dict) -> None:
    """
    Render a beautiful plan table — like 'terraform plan' output.

    Args:
        resources: list of dicts from provider.get_plan()
                   keys: resource, name, type, detail
        config_summary: dict with provider, location, vm_size keys
    """
    # Icon map for resource types
    icons = {
        "container": "📦",
        "network":   "🌐",
        "security":  "🔒",
        "compute":   "💻",
    }

    table = Table(
        title="[bold cyan]Execution Plan[/bold cyan]  [dim](no resources will be created)[/dim]",
        box=box.ROUNDED,
        border_style="cyan",
        show_header=True,
        header_style="bold white",
        padding=(0, 1),
    )
    table.add_column("  ", width=3, no_wrap=True)           # icon
    table.add_column("Resource",     style="green",  min_width=22)
    table.add_column("Name",         style="white",  min_width=24)
    table.add_column("Detail",       style="dim",    min_width=28)

    for r in resources:
        icon = icons.get(r.get("type", ""), "  ")
        table.add_row(
            f"[green]+[/green] {icon}",
            r["resource"],
            r["name"],
            r["detail"],
        )

    console.print()
    console.print(table)

    # Cost estimate panel
    vm_size = config_summary.get("vm_size", "Standard_B1s")
    hourly = _COST_MAP.get(vm_size, _DEFAULT_COST)
    monthly = hourly * 24 * 30

    cost_text = Text()
    cost_text.append("  Provider   ", style="dim")
    cost_text.append(config_summary.get("provider", "azure").upper(), style="bold cyan")
    cost_text.append("   |   ", style="dim")
    cost_text.append("Region   ", style="dim")
    cost_text.append(config_summary.get("location", "-"), style="bold white")
    cost_text.append("   |   ", style="dim")
    cost_text.append("VM Size   ", style="dim")
    cost_text.append(vm_size, style="bold white")
    cost_text.append("\n\n  Estimated cost   ", style="dim")
    cost_text.append(f"~${hourly:.4f}/hour", style="bold yellow")
    cost_text.append("   (", style="dim")
    cost_text.append(f"~${monthly:.2f}/month", style="yellow")
    cost_text.append(")", style="dim")
    cost_text.append("\n  Prices are estimates. Actual charges depend on Azure pricing.", style="dim")

    console.print(Panel(cost_text, border_style="yellow", title="[yellow]Cost Estimate[/yellow]", padding=(0, 1)))
    console.print(
        "  Run without [bold]--plan[/bold] to apply these changes.\n",
        style="dim",
    )


def print_status_panel(vm_status) -> None:
    """
    Render a VM status panel with live state indicator.
    vm_status: VMStatus dataclass from providers/base.py
    """
    is_running = vm_status.state == "running"
    state_style = "bold green" if is_running else "bold yellow"
    state_icon  = "🟢" if is_running else "🟡"

    text = Text()
    text.append(f"  {state_icon} State      ", style="dim")
    text.append(vm_status.state.upper(), style=state_style)

    text.append("\n  📍 Provider   ", style="dim")
    text.append(vm_status.provider.upper(), style="bold cyan")

    text.append("\n  🌐 Public IP  ", style="dim")
    text.append(vm_status.public_ip or "N/A", style="bold white")

    text.append("\n  📌 Location   ", style="dim")
    text.append(vm_status.location, style="white")

    text.append("\n  💻 VM Size    ", style="dim")
    text.append(vm_status.vm_size, style="white")

    if vm_status.os_disk_size_gb:
        text.append("\n  💾 OS Disk    ", style="dim")
        text.append(f"{vm_status.os_disk_size_gb} GB", style="white")

    if is_running and vm_status.public_ip:
        text.append("\n\n  🔗 ", style="dim")
        text.append(f"http://{vm_status.public_ip}", style="bold underline cyan")

    title_style = "bold green" if is_running else "bold yellow"
    console.print()
    console.print(Panel(
        text,
        title=f"[{title_style}]VM Status — {vm_status.vm_name}[/{title_style}]",
        border_style="green" if is_running else "yellow",
        padding=(0, 2),
    ))


def make_log_handler(prefix: str = "") -> Callable[[str], None]:
    """
    Returns a log callable that formats output with Rich.
    Used as the `log=` argument passed to provider methods.

    Detects line content to apply appropriate styling:
      ✅  → green
      ❌  → red
      ⏳/⚙️  → yellow
      $  (shell cmd) → dim cyan (code style)
      default → white
    """
    def _log(message: str) -> None:
        msg = str(message)
        if msg.startswith("✅") or "[OK]" in msg:
            console.print(f"  {msg}", style="green")
        elif msg.startswith("❌") or "[ERROR]" in msg:
            console.print(f"  {msg}", style="bold red")
        elif msg.startswith("⏳") or msg.startswith("⚙") or "Generating" in msg:
            console.print(f"  {msg}", style="yellow")
        elif msg.strip().startswith("$") or msg.strip().startswith("  $"):
            console.print(f"  {msg}", style="dim cyan")
        elif msg.startswith("🎉"):
            console.print(f"\n  {msg}", style="bold green")
        elif msg.startswith("🚀") or msg.startswith("☁") or msg.startswith("🔗"):
            console.print(f"  {msg}", style="cyan")
        else:
            console.print(f"  {msg}", style="white")

    return _log


def print_success(message: str) -> None:
    console.print(Panel(f"  {message}", border_style="green", padding=(0, 1)))


def print_error(message: str) -> None:
    console.print(Panel(f"  {message}", border_style="red", title="[red]Error[/red]", padding=(0, 1)))
