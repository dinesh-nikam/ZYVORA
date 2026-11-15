"""Formatting utilities for CLI output using Rich and JSON."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
err_console = Console(stderr=True)


def print_json(data: Any) -> None:
    console.print_json(data=data)


def print_error(message: str, details: Any = None) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {message}")
    if details:
        err_console.print(details)


def print_success(message: str) -> None:
    console.print(f"[bold green]Success:[/bold green] {message}")


def format_phone_validation(data: dict[str, Any]) -> None:
    table = Table(title="Phone Validation Summary", show_header=True, header_style="bold cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("E.164 Standard", data.get("e164", "N/A"))
    table.add_row("International", data.get("international", "N/A"))
    table.add_row("National", data.get("national", "N/A"))
    table.add_row("Country", f"{data.get('country_name', 'Unknown')} (+{data.get('country_code', '')})")
    table.add_row("Region Code", data.get("region_code") or "N/A")
    table.add_row("Number Type", data.get("number_type", "UNKNOWN"))

    status = data.get("validation_status", "UNKNOWN")
    status_style = "green" if status == "VALID" else ("yellow" if status == "POSSIBLE" else "red")
    table.add_row("Status", f"[{status_style}]{status}[/{status_style}]")
    table.add_row("Valid", str(data.get("is_valid", False)))
    table.add_row("Possible", str(data.get("is_possible", False)))

    console.print(table)

    limitations = data.get("limitations", [])
    if limitations:
        console.print("\n[bold yellow]Limitations & Attribution Warning:[/bold yellow]")
        for lim in limitations:
            console.print(f"  • {lim}")


def format_phone_metadata(data: dict[str, Any]) -> None:
    table = Table(title="Phone Number Metadata & Carrier Intelligence", show_header=True, header_style="bold cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("E.164 Standard", data.get("e164", "N/A"))
    table.add_row("Country", f"{data.get('country_name', 'Unknown')} (+{data.get('country_code', '')})")
    table.add_row("Region", data.get("region_description") or data.get("region_code") or "N/A")
    table.add_row("Number Type", data.get("number_type", "UNKNOWN"))
    table.add_row("Carrier", data.get("carrier") or "Unavailable / Not Allocated")
    table.add_row("Carrier Source", data.get("carrier_source") or "N/A")
    table.add_row("Metadata Provider", data.get("metadata_provider") or "N/A")

    tzs = data.get("timezones", [])
    table.add_row("Timezones", ", ".join(tzs) if tzs else "N/A")

    console.print(table)


def format_profiles_table(profiles: list[dict[str, Any]]) -> None:
    if not profiles:
        console.print("[yellow]No profiles discovered.[/yellow]")
        return

    table = Table(title="Discovered & Evaluated Social Profiles", show_header=True, header_style="bold cyan")
    table.add_column("Platform", style="bold")
    table.add_column("Canonical URL", overflow="fold")
    table.add_column("Status", overflow="fold")
    table.add_column("Confidence", overflow="fold")
    table.add_column("Evidence Summary", overflow="fold")

    for p in profiles:
        status = p.get("verification_status", "UNVERIFIED")
        style = (
            "green" if status == "VERIFIED_PUBLIC_REFERENCE"
            else ("cyan" if status == "PUBLIC_CANDIDATE" else "white")
        )
        conf = f"{p.get('confidence_level', 'NONE')} ({p.get('confidence_score', 0)}/100)"
        desc = p.get("evidence_description", "")
        summary = (desc[:60] + "...") if len(desc) > 60 else desc
        table.add_row(
            p.get("platform", "").upper(),
            p.get("canonical_url", ""),
            f"[{style}]{status}[/{style}]",
            conf,
            summary,
        )

    console.print(table)


def format_investigations_history(items: list[dict[str, Any]], total: int) -> None:
    table = Table(title=f"Investigation History (Total: {total})", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="dim", overflow="fold")
    table.add_column("Reference", style="bold", overflow="fold")
    table.add_column("Phone", overflow="fold")
    table.add_column("Basis", overflow="fold")
    table.add_column("Status", overflow="fold")
    table.add_column("Profiles")
    table.add_column("Created At", overflow="fold")

    for item in items:
        status = item.get("status", "unknown")
        style = "green" if status == "completed" else ("yellow" if status in ("running", "queued") else "red")
        table.add_row(
            item.get("id", ""),
            item.get("investigation_reference", ""),
            item.get("normalized_phone", "") or "REDACTED",
            item.get("authorization_basis", ""),
            f"[{style}]{status}[/{style}]",
            str(item.get("profile_count", 0)),
            str(item.get("created_at", ""))[:19],
        )

    console.print(table)


def format_url_verification(data: dict[str, Any]) -> None:
    status = data.get("verification_status", "UNVERIFIED")
    style = "green" if "VERIFIED" in status else "yellow"

    panel_content = (
        f"[bold]Input URL:[/bold] {data.get('input_url')}\n"
        f"[bold]Canonical URL:[/bold] {data.get('canonical_url', 'N/A')}\n"
        f"[bold]Platform:[/bold] {data.get('platform_name') or data.get('platform', 'N/A')}\n"
        f"[bold]Handle:[/bold] {data.get('handle', 'N/A')}\n"
        f"[bold]Status:[/bold] [{style}]{status}[/{style}]\n"
        f"[bold]Confidence Score:[/bold] {data.get('confidence_score', 0)}/100 ({data.get('confidence_level')})\n"
        f"[bold]Evidence:[/bold] {data.get('evidence_description', 'No direct evidence found')}"
    )
    console.print(Panel(panel_content, title="URL Verification Result", border_style="cyan"))

    stages = data.get("stages", [])
    if stages:
        table = Table(title="Pipeline Verification Stages", show_header=True, header_style="bold")
        table.add_column("Stage")
        table.add_column("Outcome")
        table.add_column("Detail")
        for s in stages:
            outcome = s.get("outcome", "")
            ostyle = "green" if outcome == "passed" else ("yellow" if outcome in ("info", "skipped") else "red")
            table.add_row(s.get("stage", ""), f"[{ostyle}]{outcome}[/{ostyle}]", s.get("detail", ""))
        console.print(table)
