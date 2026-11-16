"""PHONEINTEL command-line interface.

Works on Linux, macOS, Windows (PowerShell/cmd) and Android Termux.

Exit codes: 0 ok · 1 error · 2 usage · 3 authentication · 4 permission denied · 5 not found ·
6 validation · 7 rate limited · 8 conflict · 9 still running (wait timed out) · 10 network/server ·
130 interrupted.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import click
import typer

from phoneintel_cli import __version__
from phoneintel_cli.client import (
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_USAGE,
    APIError,
    PhoneIntelClient,
)
from phoneintel_cli.config import CLIConfig, ConfigError
from phoneintel_cli.formatters import Output

EXIT_PENDING = 9

AUTHORIZATION_BASES = [
    "number_holder_consent",
    "own_number",
    "fraud_prevention",
    "security_incident_response",
    "legal_obligation",
    "law_enforcement_request",
    "journalistic_research",
    "authorized_penetration_test",
]
PLATFORMS = [
    "telegram",
    "whatsapp",
    "instagram",
    "facebook",
    "linkedin",
    "x",
    "youtube",
    "tiktok",
    "reddit",
    "github",
    "threads",
    "snapchat",
    "pinterest",
    "vk",
    "medium",
]
TERMINAL_STATES = {"completed", "failed", "cancelled"}

Basis = StrEnum("Basis", {b: b for b in AUTHORIZATION_BASES})  # type: ignore[misc]
Platform = StrEnum("Platform", {p: p for p in PLATFORMS})  # type: ignore[misc]


class ReportFormat(StrEnum):
    JSON = "json"
    CSV = "csv"
    HTML = "html"
    PDF = "pdf"


class InvestigationState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACK_TEXT = (
    "I confirm the stated authorization basis applies to this investigation, and I understand results are "
    "public-source indications - not proof of identity or ownership."
)

app = typer.Typer(
    name="phoneintel",
    help="PHONEINTEL - phone-number intelligence and public social-profile link discovery.",
    no_args_is_help=True,
    add_completion=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Show or change CLI configuration.", no_args_is_help=True)
apikey_app = typer.Typer(help="Manage API keys for your account.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(apikey_app, name="apikey")


@dataclass
class State:
    output: Output
    config: CLIConfig
    api_key: str | None
    verbose: bool

    def client(self) -> PhoneIntelClient:
        return PhoneIntelClient(
            self.config,
            api_key=self.api_key,
            verbose=self.verbose,
            log=lambda m: self.output.err.print(f"[dim]{m}[/dim]"),
        )


def state(ctx: typer.Context) -> State:
    return ctx.ensure_object(State)


@contextmanager
def api_errors(st: State) -> Iterator[None]:
    try:
        yield
    except APIError as exc:
        st.output.error(exc.message, details=exc.details, request_id=exc.request_id)
        if exc.status_code == 401 and not st.api_key:
            st.output.info("Run 'phoneintel login' (or set PHONEINTEL_API_KEY) to authenticate.")
        raise typer.Exit(exc.exit_code) from exc
    except ConfigError as exc:
        st.output.error(str(exc))
        raise typer.Exit(EXIT_USAGE) from exc


def version_callback(value: bool) -> None:
    if value:
        sys.stdout.write(f"phoneintel {__version__}\n")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    api_url: Annotated[
        str | None, typer.Option("--api-url", envvar="PHONEINTEL_API_URL", help="API base URL (default from config)")
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            envvar="PHONEINTEL_API_KEY",
            show_envvar=True,
            help="API key (prefer the env variable over the flag)",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable JSON on stdout")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Minimal tab-separated output")] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Debug output (requests, timings) on stderr")
    ] = False,
    no_color: Annotated[bool, typer.Option("--no-color", envvar="NO_COLOR", help="Disable colours")] = False,
    _version: Annotated[
        bool, typer.Option("--version", callback=version_callback, is_eager=True, help="Show version and exit")
    ] = False,
) -> None:
    """PHONEINTEL CLI.  Authenticate with 'phoneintel login' or PHONEINTEL_API_KEY."""
    config = CLIConfig.load()
    if api_url:
        config.api_url = api_url.rstrip("/")
    output = Output(json_mode=json_output, quiet=quiet, no_color=no_color)
    for warning in config.warnings:
        output.warn(warning)
    if config.api_url.startswith("http://") and not config.api_url.startswith(("http://localhost", "http://127.0.0.1")):
        output.warn("API URL uses plain HTTP; credentials would be sent unencrypted. Use https:// in production.")
    ctx.obj = State(output=output, config=config, api_key=api_key, verbose=verbose)


# ----------------------------------------------------------------- account
@app.command()
def login(
    ctx: typer.Context,
    email: Annotated[str, typer.Option("--email", "-e", prompt=True)],
    password: Annotated[
        str, typer.Option("--password", prompt=True, hide_input=True, help="Prompted when omitted (recommended)")
    ],
) -> None:
    """Log in and store a session (access + rotating refresh token) in the config file."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        client.login(email, password)
    st.output.success(f"Logged in as {email}")


@app.command()
def logout(
    ctx: typer.Context,
    all_sessions: Annotated[bool, typer.Option("--all", help="Revoke every session of this account")] = False,
) -> None:
    """Revoke the stored session on the server and remove it locally."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        client.logout(all_sessions=all_sessions)
    st.output.success("Logged out")


@app.command()
def register(
    ctx: typer.Context,
    email: Annotated[str, typer.Option("--email", "-e", prompt=True)],
    password: Annotated[str, typer.Option("--password", prompt=True, hide_input=True, confirmation_prompt=True)],
) -> None:
    """Create an account (may require administrator approval)."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        user = client.register(email, password)
    if st.output.json_mode:
        st.output.data(user)
    else:
        st.output.success(f"Account created for {user['email']} (status: {user['status']})")


@app.command()
def whoami(ctx: typer.Context) -> None:
    """Show the authenticated account and its permissions."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        me = client.me()
    if st.output.json_mode:
        st.output.data(me)
    elif st.output.quiet:
        st.output.line(me["email"])
    else:
        st.output.console.print(f"[bold]{me['email']}[/bold] role={me['role']} via={me['authenticated_via']}")
        st.output.console.print("permissions: " + ", ".join(me["permissions"]))


@app.command("link-telegram")
def link_telegram(ctx: typer.Context) -> None:
    """Create a one-time code to link your Telegram account to the PHONEINTEL bot."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        data = client.telegram_link_code()
    if st.output.json_mode:
        st.output.data(data)
    else:
        st.output.console.print(
            f"Link code: [bold yellow]{data['code']}[/bold yellow] (expires in {data['expires_in'] // 60} min)"
        )
        st.output.console.print(data["instructions"])


# ------------------------------------------------------------------ config
@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Show configuration (secrets are masked)."""
    st = state(ctx)
    data = st.config.redacted()
    if st.output.json_mode:
        st.output.data(data)
        return
    for key, value in data.items():
        st.output.console.print(f"[bold]{key}[/bold] = {value}")


@config_app.command("set")
def config_set(ctx: typer.Context, key: str, value: str) -> None:
    """Set a configuration value (api_url, api_key, timeout_seconds, verify_tls)."""
    st = state(ctx)
    with api_errors(st):
        st.config.set_value(key, value)
        st.config.save()
    st.output.success(f"{key} updated")


@config_app.command("path")
def config_path(ctx: typer.Context) -> None:
    """Print the configuration file location."""
    sys.stdout.write(str(CLIConfig.path()) + "\n")


# ----------------------------------------------------------------- API keys
@apikey_app.command("create")
def apikey_create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Label for the key")],
    permission: Annotated[
        list[str] | None, typer.Option("--permission", "-p", help="Restrict to permission (repeatable)")
    ] = None,
    expires_in_days: Annotated[int, typer.Option(min=1, max=365)] = 90,
) -> None:
    """Create an API key.  The key is displayed once - store it in a secret manager."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        data = client.create_api_key(name, permission, expires_in_days)
    if st.output.json_mode:
        st.output.data(data)
        return
    st.output.success(f"API key '{data['name']}' created (expires {str(data['expires_at'])[:10]})")
    st.output.console.print("Copy it now; it cannot be shown again:")
    sys.stdout.write(data["api_key"] + "\n")


@apikey_app.command("list")
def apikey_list(ctx: typer.Context) -> None:
    """List API keys (prefixes only)."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        keys = client.list_api_keys()
    if st.output.json_mode:
        st.output.data(keys)
        return
    for key in keys:
        status = "revoked" if key["revoked_at"] else "active"
        st.output.console.print(
            f"{key['id']}  pi_{key['key_prefix']}_…  {key['name']}  {status}  expires={str(key['expires_at'])[:10]}"
        )


@apikey_app.command("revoke")
def apikey_revoke(ctx: typer.Context, key_id: str) -> None:
    """Revoke an API key by id."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        client.revoke_api_key(key_id)
    st.output.success("API key revoked")


# ------------------------------------------------------- phone intelligence
RegionOpt = Annotated[str | None, typer.Option("--region", "-r", help="ISO region hint for national-format input")]

_VALIDATE_FIELDS = [
    ("E.164", "e164"),
    ("International", "international"),
    ("National", "national"),
    ("Country", "country_name"),
    ("Country code", "country_code"),
    ("Region", "region_code"),
    ("Number type", "number_type"),
    ("Validation", "validation_status"),
    ("Possibility", "possibility_reason"),
]


@app.command()
def validate(ctx: typer.Context, phone: str, region: RegionOpt = None) -> None:
    """Parse, normalise (E.164) and validate a number.  The number is not stored."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        data = client.validate_phone(phone, region)
    st.output.phone(data, title="Phone validation", fields=_VALIDATE_FIELDS)
    if not data.get("is_valid"):
        raise typer.Exit(EXIT_ERROR)


@app.command()
def country(ctx: typer.Context, phone: str, region: RegionOpt = None) -> None:
    """Show the country / numbering-plan region of a number."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        data = client.phone_metadata(phone, region)
    st.output.phone(
        data,
        title="Country",
        fields=[
            ("E.164", "e164"),
            ("Country", "country_name"),
            ("Country code", "country_code"),
            ("Region code", "region_code"),
            ("Region description", "region_description"),
            ("Time zones", "timezones"),
        ],
    )


@app.command()
def carrier(ctx: typer.Context, phone: str, region: RegionOpt = None) -> None:
    """Show carrier (original allocation or licensed lookup) and number type."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        data = client.phone_metadata(phone, region)
    st.output.phone(
        data,
        title="Carrier",
        fields=[
            ("E.164", "e164"),
            ("Carrier", "carrier"),
            ("Carrier source", "carrier_source"),
            ("Number type", "number_type"),
            ("Validation", "validation_status"),
            ("Metadata provider", "metadata_provider"),
        ],
    )


# ---------------------------------------------------------------- discovery
def _require_authorization(st: State, purpose: str | None, basis: str | None, ack: bool) -> tuple[str, str]:
    interactive = sys.stdin.isatty() and not st.output.json_mode
    if not basis:
        if not interactive:
            st.output.error("--basis is required", details=[f"one of: {', '.join(AUTHORIZATION_BASES)}"])
            raise typer.Exit(EXIT_USAGE)
        basis = typer.prompt("Authorization basis", type=click.Choice(AUTHORIZATION_BASES))
    if not purpose:
        if not interactive:
            st.output.error("--purpose is required (at least 10 characters)")
            raise typer.Exit(EXIT_USAGE)
        purpose = typer.prompt("Purpose of this investigation")
    if not ack:
        if not interactive:
            st.output.error("--ack is required", details=[ACK_TEXT])
            raise typer.Exit(EXIT_USAGE)
        if not typer.confirm(ACK_TEXT):
            st.output.error("Acknowledgement declined; nothing was created")
            raise typer.Exit(EXIT_USAGE)
    if purpose is None or basis is None:  # unreachable: both were prompted for above
        raise typer.Exit(EXIT_USAGE)
    return purpose, basis


def _wait(st: State, client: PhoneIntelClient, inv_id: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    delay = 1.0
    detail = client.get_investigation(inv_id)
    status_ctx: Any = None
    if not st.output.quiet and not st.output.json_mode:
        status_ctx = st.output.err.status("Discovery running…")
        status_ctx.__enter__()
    try:
        while detail["investigation"]["status"] not in TERMINAL_STATES and time.monotonic() < deadline:
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)
            detail = client.get_investigation(inv_id)
            if status_ctx is not None:
                job = detail.get("latest_job") or {}
                status_ctx.update(
                    f"Discovery {detail['investigation']['status']} "
                    f"(job {job.get('status', '?')}, retries {job.get('retry_count', 0)}) - "
                    "Ctrl+C cancels"
                )
    except KeyboardInterrupt:
        if status_ctx is not None:
            status_ctx.__exit__(None, None, None)
            status_ctx = None
        st.output.warn("Interrupted - requesting cancellation of the discovery job")
        try:
            client.cancel_investigation(inv_id)
        except APIError as exc:
            st.output.warn(f"Cancellation request failed: {exc.message}")
        raise typer.Exit(EXIT_INTERRUPTED) from None
    finally:
        if status_ctx is not None:
            status_ctx.__exit__(None, None, None)
    return detail


def _discover(
    ctx: typer.Context,
    phone: str,
    *,
    region: str | None,
    purpose: str | None,
    basis: Basis | None,
    reference: str | None,
    ack: bool,
    platforms: list[Platform] | None,
    urls: list[str] | None,
    live_checks: bool,
    web_search: bool,
    wait: bool,
    timeout: int,
) -> None:
    st = state(ctx)
    purpose, basis_value = _require_authorization(st, purpose, basis.value if basis else None, ack)
    platform_values = [p.value for p in platforms] if platforms else None
    with api_errors(st), st.client() as client:
        inv = client.create_investigation(phone, purpose=purpose, basis=basis_value, region=region, reference=reference)
        st.config.last_investigation_id = inv["id"]
        st.config.save()
        st.output.info(f"Investigation {inv['investigation_reference']} created ({inv['id']})")
        job = client.start_discovery(
            inv["id"],
            platforms=platform_values,
            profile_urls=urls,
            include_web_search=web_search,
            live_checks=live_checks,
        )
        if not wait:
            if st.output.json_mode:
                st.output.data({"investigation": inv, "job": job})
            else:
                st.output.investigation(inv, job)
                st.output.info(f"Queued. Check progress with: phoneintel status {inv['id']}")
            return
        detail = _wait(st, client, inv["id"], timeout)
        profiles = client.list_profiles(inv["id"])["items"]
    investigation = detail["investigation"]
    if st.output.json_mode:
        st.output.data({"investigation": investigation, "latest_job": detail.get("latest_job"), "profiles": profiles})
    else:
        st.output.investigation(investigation, detail.get("latest_job"))
        st.output.profiles(profiles, investigation.get("summary"))
        st.output.info(f"Report: phoneintel report --id {inv['id']} --format pdf")
    if investigation["status"] == "failed":
        raise typer.Exit(EXIT_ERROR)
    if investigation["status"] not in TERMINAL_STATES:
        st.output.warn(f"Still running after {timeout}s; check later with 'phoneintel status {inv['id']}'")
        raise typer.Exit(EXIT_PENDING)


PurposeOpt = Annotated[str | None, typer.Option("--purpose", "-p", help="Why this investigation is necessary")]
BasisOpt = Annotated[Basis | None, typer.Option("--basis", "-b", help="Authorization / lawful basis")]
RefOpt = Annotated[str | None, typer.Option("--reference", help="Case, ticket or consent reference")]
AckOpt = Annotated[bool, typer.Option("--ack", help="Acknowledge authorization and result limitations")]
PlatformOpt = Annotated[list[Platform] | None, typer.Option("--platform", help="Limit to platform (repeatable)")]
UrlOpt = Annotated[list[str] | None, typer.Option("--url", "-u", help="Profile URL you already know (repeatable)")]
WaitOpt = Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for the background job")]
TimeoutOpt = Annotated[int, typer.Option("--timeout", min=5, max=3600, help="Seconds to wait")]


@app.command("profile-links")
def profile_links(
    ctx: typer.Context,
    phone: str,
    region: RegionOpt = None,
    purpose: PurposeOpt = None,
    basis: BasisOpt = None,
    reference: RefOpt = None,
    ack: AckOpt = False,
    platform: PlatformOpt = None,
    url: UrlOpt = None,
    live: Annotated[
        bool, typer.Option("--live/--no-live", help="Permitted live checks (official APIs, robots-permitted pages)")
    ] = True,
    wait: WaitOpt = True,
    timeout: TimeoutOpt = 300,
) -> None:
    """Discover public, evidence-backed social profile links for a number."""
    _discover(
        ctx,
        phone,
        region=region,
        purpose=purpose,
        basis=basis,
        reference=reference,
        ack=ack,
        platforms=platform,
        urls=url,
        live_checks=live,
        web_search=True,
        wait=wait,
        timeout=timeout,
    )


@app.command("public-search")
def public_search(
    ctx: typer.Context,
    phone: str,
    region: RegionOpt = None,
    purpose: PurposeOpt = None,
    basis: BasisOpt = None,
    reference: RefOpt = None,
    ack: AckOpt = False,
    platform: PlatformOpt = None,
    wait: WaitOpt = True,
    timeout: TimeoutOpt = 300,
) -> None:
    """Search authorised public web indexes only (no live profile checks)."""
    _discover(
        ctx,
        phone,
        region=region,
        purpose=purpose,
        basis=basis,
        reference=reference,
        ack=ack,
        platforms=platform,
        urls=None,
        live_checks=False,
        web_search=True,
        wait=wait,
        timeout=timeout,
    )


@app.command("verify-url")
def verify_url(
    ctx: typer.Context,
    url: str,
    phone: Annotated[str | None, typer.Option("--phone", help="Number to look for on the profile")] = None,
) -> None:
    """Run the 10-stage verification pipeline on one profile URL."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        data = client.verify_url(url, phone)
    st.output.verification(data)
    if data["verification_status"] == "ERROR":
        raise typer.Exit(EXIT_ERROR)


def _resolve_id(st: State, investigation_id: str | None) -> str:
    resolved = investigation_id or st.config.last_investigation_id
    if not resolved:
        st.output.error("No investigation id given and no previous investigation recorded; use --id")
        raise typer.Exit(EXIT_USAGE)
    return resolved


IdOpt = Annotated[str | None, typer.Option("--id", help="Investigation id (default: most recent from this CLI)")]


@app.command()
def status(
    ctx: typer.Context,
    investigation_id: IdOpt = None,
    profiles: Annotated[bool, typer.Option("--profiles", help="Also list profiles")] = False,
) -> None:
    """Show investigation and job status."""
    st = state(ctx)
    inv_id = _resolve_id(st, investigation_id)
    with api_errors(st), st.client() as client:
        detail = client.get_investigation(inv_id)
        items = client.list_profiles(inv_id)["items"] if profiles else None
    if st.output.json_mode:
        st.output.data({**detail, "profiles": items})
        return
    st.output.investigation(detail["investigation"], detail.get("latest_job"))
    if items is not None:
        st.output.profiles(items, detail["investigation"].get("summary"))


@app.command()
def cancel(ctx: typer.Context, investigation_id: IdOpt = None) -> None:
    """Cancel a running discovery job."""
    st = state(ctx)
    inv_id = _resolve_id(st, investigation_id)
    with api_errors(st), st.client() as client:
        client.cancel_investigation(inv_id)
    st.output.success("Cancellation requested")


@app.command()
def report(
    ctx: typer.Context,
    investigation_id: IdOpt = None,
    fmt: Annotated[ReportFormat, typer.Option("--format", "-f")] = ReportFormat.JSON,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write to this file")] = None,
    mask_phone: Annotated[bool, typer.Option("--mask-phone", help="Mask the number in the report")] = False,
) -> None:
    """Generate an investigation report (JSON, CSV, HTML or PDF)."""
    st = state(ctx)
    inv_id = _resolve_id(st, investigation_id)
    with api_errors(st), st.client() as client:
        content, filename = client.export_report(inv_id, fmt.value, mask_phone)
    if output is None and fmt in (ReportFormat.JSON, ReportFormat.CSV):
        sys.stdout.write(content.decode("utf-8-sig"))
        if not content.endswith(b"\n"):
            sys.stdout.write("\n")
        return
    target = output or Path(filename)
    try:
        target.write_bytes(content)
    except OSError as exc:
        st.output.error(f"Cannot write {target}: {exc}")
        raise typer.Exit(EXIT_ERROR) from exc
    if st.output.json_mode:
        st.output.data({"file": str(target.resolve()), "format": fmt.value, "bytes": len(content)})
    else:
        st.output.success(f"Report written to {target.resolve()} ({len(content)} bytes)")


@app.command()
def history(
    ctx: typer.Context,
    page: Annotated[int, typer.Option(min=1)] = 1,
    limit: Annotated[int, typer.Option("--limit", "-l", min=1, max=100)] = 20,
    status_filter: Annotated[InvestigationState | None, typer.Option("--status")] = None,
    phone: Annotated[str | None, typer.Option("--phone", help="Only investigations of this number")] = None,
) -> None:
    """List your investigations (newest first)."""
    st = state(ctx)
    with api_errors(st), st.client() as client:
        data = client.list_investigations(
            page=page, page_size=limit, status=status_filter.value if status_filter else None, phone=phone
        )
    st.output.history(data)


@app.command("delete-data")
def delete_data(
    ctx: typer.Context,
    investigation_id: Annotated[str | None, typer.Argument(help="Investigation id to erase")] = None,
    erase_all: Annotated[bool, typer.Option("--all", help="Erase ALL of your investigation data")] = False,
    include_account: Annotated[
        bool, typer.Option("--include-account", help="With --all: also close and anonymise the account")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")] = False,
) -> None:
    """Permanently erase one investigation (or all your data with --all)."""
    st = state(ctx)
    if bool(investigation_id) == erase_all:
        st.output.error("Give exactly one of: an investigation id, or --all")
        raise typer.Exit(EXIT_USAGE)
    if include_account and not erase_all:
        st.output.error("--include-account requires --all")
        raise typer.Exit(EXIT_USAGE)
    what = (
        f"investigation {investigation_id}"
        if investigation_id
        else "ALL your investigations" + (" and close your account" if include_account else "")
    )
    if not yes:
        if not sys.stdin.isatty():
            st.output.error("Refusing to erase without confirmation; pass --yes")
            raise typer.Exit(EXIT_USAGE)
        if not typer.confirm(f"Permanently erase {what}? This cannot be undone"):
            st.output.info("Aborted; nothing was erased")
            return
    with api_errors(st), st.client() as client:
        if investigation_id:
            client.delete_investigation(investigation_id)
            result: dict[str, Any] = {"erased": investigation_id}
        else:
            result = client.erase_my_data(include_account)
            if include_account:
                st.config.clear_session()
    if st.config.last_investigation_id == investigation_id or erase_all:
        st.config.last_investigation_id = None
    st.config.save()
    if st.output.json_mode:
        st.output.data(result)
    else:
        st.output.success(f"Erased {what}")


def run() -> None:
    app()


if __name__ == "__main__":
    run()
