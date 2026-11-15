"""PHONEINTEL CLI - Command Line Interface for Phone Intelligence & Public Profile Discovery."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from phoneintel_cli.client import APIError, PhoneIntelClient
from phoneintel_cli.config import CLIConfig
from phoneintel_cli.formatters import (
    console,
    format_investigations_history,
    format_phone_metadata,
    format_phone_validation,
    format_profiles_table,
    format_url_verification,
    print_error,
    print_json,
    print_success,
)

app = typer.Typer(
    name="phoneintel",
    help="PHONEINTEL: Phone-number intelligence and public social-profile link discovery CLI.",
    add_completion=False,
    no_args_is_help=True,
)


def get_client(api_url: str | None, api_key: str | None, token: str | None) -> PhoneIntelClient:
    cfg = CLIConfig.load()
    if api_url:
        cfg.api_url = api_url.rstrip("/")
    if api_key:
        cfg.api_key = api_key
    if token:
        cfg.access_token = token
    return PhoneIntelClient(cfg)


@app.command("validate")
def validate_command(
    phone: Annotated[str, typer.Argument(help="Phone number to validate (e.g. +919876543210)")],
    region: Annotated[
        str | None, typer.Option("--region", "-r", help="2-letter ISO region code hint (e.g. IN, US)")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output raw JSON response")] = False,
    api_url: Annotated[str | None, typer.Option("--api-url", help="API base URL")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API Key for authentication")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer access token")] = None,
) -> None:
    """Validate and normalise an international phone number (E.164 standard)."""
    client = get_client(api_url, api_key, token)
    try:
        data = client.validate_phone(phone, region)
        if json_output:
            print_json(data)
        else:
            format_phone_validation(data)
    except APIError as e:
        print_error(str(e), e.details)
        sys.exit(1)


@app.command("country")
def country_command(
    phone: Annotated[str, typer.Argument(help="Phone number (e.g. +919876543210)")],
    region: Annotated[str | None, typer.Option("--region", "-r", help="2-letter ISO region code hint")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output raw JSON response")] = False,
    api_url: Annotated[str | None, typer.Option("--api-url", help="API base URL")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API Key")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer access token")] = None,
) -> None:
    """Lookup country, numbering plan region, and timezones for a phone number."""
    client = get_client(api_url, api_key, token)
    try:
        data = client.validate_phone(phone, region)
        res = {
            "e164": data.get("e164"),
            "country_name": data.get("country_name"),
            "country_code": data.get("country_code"),
            "region_code": data.get("region_code"),
            "number_type": data.get("number_type"),
        }
        if json_output:
            print_json(res)
        else:
            console.print(f"[bold cyan]Number:[/bold cyan] {res['e164']}")
            console.print(f"[bold cyan]Country:[/bold cyan] {res['country_name']} (+{res['country_code']})")
            console.print(f"[bold cyan]Region Code:[/bold cyan] {res['region_code'] or 'N/A'}")
            console.print(f"[bold cyan]Type:[/bold cyan] {res['number_type']}")
    except APIError as e:
        print_error(str(e), e.details)
        sys.exit(1)


@app.command("carrier")
def carrier_command(
    phone: Annotated[str, typer.Argument(help="Phone number to check carrier for")],
    region: Annotated[str | None, typer.Option("--region", "-r", help="2-letter ISO region code hint")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output raw JSON response")] = False,
    api_url: Annotated[str | None, typer.Option("--api-url", help="API base URL")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API Key")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer access token")] = None,
) -> None:
    """Lookup carrier, network operator, and numbering-plan metadata."""
    client = get_client(api_url, api_key, token)
    try:
        data = client.phone_metadata(phone, region)
        if json_output:
            print_json(data)
        else:
            format_phone_metadata(data)
    except APIError as e:
        print_error(str(e), e.details)
        sys.exit(1)


@app.command("profile-links")
def profile_links_command(
    phone: Annotated[str, typer.Argument(help="Phone number to discover public profiles for")],
    region: Annotated[str | None, typer.Option("--region", "-r", help="2-letter ISO region code hint")] = None,
    purpose: Annotated[str | None, typer.Option("--purpose", "-p", help="Investigation purpose description")] = None,
    basis: Annotated[
        str | None,
        typer.Option("--basis", "-b", help="Authorization basis (e.g. fraud_prevention, incident_response)"),
    ] = None,
    reference: Annotated[str | None, typer.Option("--reference", help="Case reference or ticket ID")] = None,
    urls: Annotated[list[str] | None, typer.Option("--url", "-u", help="Specific profile URLs to verify")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for discovery background job to finish")] = True,
    json_output: Annotated[bool, typer.Option("--json", help="Output raw JSON response")] = False,
    api_url: Annotated[str | None, typer.Option("--api-url", help="API base URL")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API Key")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer access token")] = None,
) -> None:
    """Discover and evaluate public social profiles associated with a phone number."""
    client = get_client(api_url, api_key, token)
    try:
        if not json_output:
            console.print(f"[bold]Opening investigation for[/bold] {phone}...")
        inv = client.create_investigation(phone, region=region, purpose=purpose, basis=basis, reference=reference)
        inv_id = inv["id"]

        if not json_output:
            ref = inv["investigation_reference"]
            console.print(f"[green]Investigation created:[/green] [bold]{ref}[/bold] (ID: {inv_id})")
            console.print("[bold]Queuing profile discovery job...[/bold]")

        job = client.start_discovery(inv_id, profile_urls=urls)

        if wait:
            if not json_output:
                console.print("[dim]Awaiting job completion...[/dim]")
            for _ in range(60):
                status_data = client.get_investigation(inv_id)
                inv_status = status_data.get("investigation", {}).get("status")
                if inv_status in ("completed", "failed", "cancelled"):
                    break
                time.sleep(1)

            profiles_data = client.list_profiles(inv_id)
            if json_output:
                print_json({"investigation": inv, "profiles": profiles_data.get("items", [])})
            else:
                format_profiles_table(profiles_data.get("items", []))
        elif json_output:
            print_json({"investigation": inv, "job": job})
        else:
            j_stat = job.get("status")
            console.print(f"[yellow]Job queued (Status: {j_stat}). Use 'phoneintel history' to check status.[/yellow]")
    except APIError as e:
        print_error(str(e), e.details)
        sys.exit(1)


@app.command("public-search")
def public_search_command(
    phone: Annotated[str, typer.Argument(help="Phone number to search public web for")],
    region: Annotated[str | None, typer.Option("--region", "-r", help="Region code hint")] = None,
    purpose: Annotated[str | None, typer.Option("--purpose", "-p", help="Purpose of search")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output raw JSON")] = False,
    api_url: Annotated[str | None, typer.Option("--api-url", help="API base URL")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API Key")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer access token")] = None,
) -> None:
    """Execute public-source discovery and display public candidates."""
    # Delegates to profile_links with full web search enabled
    profile_links_command(
        phone=phone,
        region=region,
        purpose=purpose or "Public search investigation",
        basis="incident_response",
        reference=None,
        urls=None,
        wait=True,
        json_output=json_output,
        api_url=api_url,
        api_key=api_key,
        token=token,
    )


@app.command("verify-url")
def verify_url_command(
    url: Annotated[str, typer.Argument(help="Social profile URL to verify (e.g. https://t.me/example)")],
    phone: Annotated[
        str | None, typer.Option("--phone", "-p", help="Optional phone number to check for in public metadata")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output raw JSON response")] = False,
    api_url: Annotated[str | None, typer.Option("--api-url", help="API base URL")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API Key")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer access token")] = None,
) -> None:
    """Run the 10-stage verification pipeline on an individual candidate URL."""
    client = get_client(api_url, api_key, token)
    try:
        data = client.verify_url(url, phone)
        if json_output:
            print_json(data)
        else:
            format_url_verification(data)
    except APIError as e:
        print_error(str(e), e.details)
        sys.exit(1)


@app.command("report")
def report_command(
    investigation_id: Annotated[str, typer.Argument(help="Investigation UUID")],
    format_opt: Annotated[str, typer.Option("--format", "-f", help="Report format: json, csv, pdf, html")] = "json",
    output: Annotated[Path | None, typer.Option("--output", "-o", help="File path to save the report")] = None,
    mask_phone: Annotated[bool, typer.Option("--mask-phone", help="Mask the phone number for privacy")] = False,
    api_url: Annotated[str | None, typer.Option("--api-url", help="API base URL")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API Key")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer access token")] = None,
) -> None:
    """Generate and download or view an investigation report."""
    client = get_client(api_url, api_key, token)
    fmt = format_opt.lower().strip()
    try:
        if fmt == "json" and not output:
            data = client.get_report(investigation_id, mask_phone=mask_phone)
            print_json(data)
            return

        content = client.export_report(investigation_id, fmt=fmt, mask_phone=mask_phone)
        if output:
            output.write_bytes(content)
            print_success(f"Report saved to {output.resolve()}")
        elif fmt in ("json", "csv", "html"):
            sys.stdout.write(content.decode("utf-8", errors="replace"))
        else:
            out_path = Path(f"report_{investigation_id[:8]}.pdf")
            out_path.write_bytes(content)
            print_success(f"PDF report saved to {out_path.resolve()}")
    except APIError as e:
        print_error(str(e), e.details)
        sys.exit(1)


@app.command("history")
def history_command(
    page: Annotated[int, typer.Option("--page", help="Page number")] = 1,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Number of records to show")] = 20,
    status: Annotated[
        str | None, typer.Option("--status", "-s", help="Filter by status (e.g. completed, failed)")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output raw JSON")] = False,
    api_url: Annotated[str | None, typer.Option("--api-url", help="API base URL")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API Key")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer access token")] = None,
) -> None:
    """View past investigations and their current statuses."""
    client = get_client(api_url, api_key, token)
    try:
        data = client.list_investigations(page=page, page_size=limit, status=status)
        if json_output:
            print_json(data)
        else:
            format_investigations_history(data.get("items", []), data.get("total", 0))
    except APIError as e:
        print_error(str(e), e.details)
        sys.exit(1)


@app.command("delete-data")
def delete_data_command(
    investigation_id: Annotated[str, typer.Argument(help="Investigation UUID to delete")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
    api_url: Annotated[str | None, typer.Option("--api-url", help="API base URL")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API Key")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer access token")] = None,
) -> None:
    """Immediately erase all phone, profile, and evidence data for an investigation."""
    if not yes:
        confirm = typer.confirm(f"Are you sure you want to permanently erase investigation {investigation_id}?")
        if not confirm:
            console.print("[yellow]Deletion aborted.[/yellow]")
            return

    client = get_client(api_url, api_key, token)
    try:
        client.delete_investigation(investigation_id)
        print_success(f"Investigation {investigation_id} and all associated evidence permanently deleted.")
    except APIError as e:
        print_error(str(e), e.details)
        sys.exit(1)


@app.command("login")
def login_command(
    email: Annotated[str, typer.Option("--email", "-e", prompt=True, help="User email address")],
    password: Annotated[str, typer.Option("--password", "-p", prompt=True, hide_input=True, help="User password")],
    api_url: Annotated[str | None, typer.Option("--api-url", help="API base URL")] = None,
) -> None:
    """Authenticate and store access credentials locally in ~/.phoneintel/config.json."""
    client = get_client(api_url, None, None)
    try:
        client.login(email, password)
        print_success(f"Successfully logged in as {email}!")
    except APIError as e:
        print_error(str(e), e.details)
        sys.exit(1)


@app.command("link-telegram")
def link_telegram_command(
    api_url: Annotated[str | None, typer.Option("--api-url", help="API base URL")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API Key")] = None,
    token: Annotated[str | None, typer.Option("--token", help="Bearer access token")] = None,
) -> None:
    """Generate a one-time code to link your Telegram account to PHONEINTEL."""
    client = get_client(api_url, api_key, token)
    try:
        data = client.create_telegram_link_code()
        console.print(f"[bold green]One-Time Link Code:[/bold green] [bold yellow]{data.get('code')}[/bold yellow]")
        console.print(f"[cyan]Expires in:[/cyan] {data.get('expires_in', 600)} seconds")
        console.print(f"[white]{data.get('instructions')}[/white]")
    except APIError as e:
        print_error(str(e), e.details)
        sys.exit(1)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
