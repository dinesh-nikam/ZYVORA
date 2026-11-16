"""Rich terminal rendering with colour-coded statuses, plus JSON and quiet modes."""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

STATUS_STYLE = {
    "VERIFIED_PUBLIC_REFERENCE": "bold green",
    "PUBLIC_CANDIDATE": "yellow",
    "UNVERIFIED": "white",
    "INACCESSIBLE": "magenta",
    "NOT_FOUND": "dim",
    "RESTRICTED": "cyan",
    "ERROR": "bold red",
    "VALID": "bold green",
    "POSSIBLE_NOT_VALID": "yellow",
    "INVALID": "bold red",
    "completed": "green",
    "running": "cyan",
    "queued": "cyan",
    "created": "white",
    "failed": "bold red",
    "cancelled": "yellow",
}
CONFIDENCE_STYLE = {"HIGH": "bold green", "MEDIUM": "yellow", "LOW": "white", "NONE": "dim"}


def styled(value: Any, styles: dict[str, str] = STATUS_STYLE) -> str:
    text = escape(str(value if value is not None else "-"))
    style = styles.get(str(value))
    return f"[{style}]{text}[/{style}]" if style else text


class Output:
    """Single place that decides how (and whether) things are printed."""

    def __init__(self, *, json_mode: bool = False, quiet: bool = False, no_color: bool = False) -> None:
        self.json_mode = json_mode
        self.quiet = quiet
        self.console = Console(no_color=no_color, highlight=False, soft_wrap=False)
        self.err = Console(stderr=True, no_color=no_color, highlight=False)

    # ----------------------------------------------------------- primitives
    def data(self, payload: Any) -> None:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n")

    def line(self, text: str) -> None:
        """Raw, unstyled line on stdout (quiet mode is meant for scripts: tabs are preserved)."""
        sys.stdout.write(text + "\n")

    def info(self, message: str) -> None:
        if not self.quiet and not self.json_mode:
            self.err.print(message)

    def success(self, message: str) -> None:
        if not self.quiet and not self.json_mode:
            self.console.print(f"[bold green]✔[/bold green] {message}")

    def warn(self, message: str) -> None:
        self.err.print(f"[yellow]warning:[/yellow] {escape(message)}")

    def error(self, message: str, *, details: Any = None, request_id: str | None = None) -> None:
        if self.json_mode:
            sys.stderr.write(
                json.dumps({"error": {"message": message, "details": details, "request_id": request_id}}) + "\n"
            )
            return
        self.err.print(f"[bold red]error:[/bold red] {escape(message)}")
        if details:
            for item in details if isinstance(details, list) else [details]:
                self.err.print(f"  • {escape(json.dumps(item, default=str) if not isinstance(item, str) else item)}")
        if request_id:
            self.err.print(f"  [dim]request id: {request_id}[/dim]")

    def table(self, table: Table) -> None:
        self.console.print(table)

    # ------------------------------------------------------------- renderers
    def phone(self, data: dict[str, Any], *, title: str, fields: list[tuple[str, str]]) -> None:
        if self.json_mode:
            self.data(data)
            return
        if self.quiet:
            self.line(f"{data.get('e164')}\t{data.get('validation_status')}\t{data.get('region_code') or '-'}")
            return
        table = Table(title=title, show_header=False, title_justify="left")
        table.add_column("Field", style="bold")
        table.add_column("Value")
        for label, key in fields:
            value = data.get(key)
            if isinstance(value, list):
                value = ", ".join(map(str, value)) or "-"
            table.add_row(
                label,
                styled(value) if key == "validation_status" else escape(str(value if value not in (None, "") else "-")),
            )
        self.table(table)
        self.limitations(data.get("limitations") or [])

    def limitations(self, items: list[str]) -> None:
        if items and not self.quiet:
            self.console.print("[bold yellow]Limitations[/bold yellow]")
            for item in items:
                self.console.print(f"  • {escape(item)}")

    def investigation(self, inv: dict[str, Any], job: dict[str, Any] | None = None) -> None:
        if self.json_mode:
            self.data({"investigation": inv, "latest_job": job})
            return
        if self.quiet:
            self.line(f"{inv['id']}\t{inv['status']}")
            return
        intel = inv.get("phone_intelligence") or {}
        lines = [
            f"[bold]Reference:[/bold] {escape(inv['investigation_reference'])}   [bold]ID:[/bold] {inv['id']}",
            f"[bold]Status:[/bold] {styled(inv['status'])}   [bold]Basis:[/bold] {escape(inv['authorization_basis'])}",
            f"[bold]Number:[/bold] {escape(str(inv.get('normalized_phone')))}   "
            f"[bold]Country:[/bold] {escape(str(intel.get('country_name')))} (+{intel.get('country_code')})   "
            f"[bold]Type:[/bold] {escape(str(intel.get('number_type')))}",
        ]
        if job:
            lines.append(
                f"[bold]Job:[/bold] {styled(job['status'])} (retries {job.get('retry_count', 0)})"
                + (f" error={escape(job['error_code'])}" if job.get("error_code") else "")
            )
        self.console.print(Panel("\n".join(lines), title="Investigation", expand=False))

    def profiles(self, items: list[dict[str, Any]], summary: dict[str, Any] | None = None) -> None:
        if self.json_mode:
            self.data({"profiles": items, "summary": summary})
            return
        if self.quiet:
            for p in items:
                self.line(f"{p['verification_status']}\t{p['confidence_level']}\t{p['canonical_url']}")
            return
        if not items:
            self.console.print("[dim]No profile links were found in permitted public sources.[/dim]")
        else:
            table = Table(title="Social profile links", show_lines=False)
            table.add_column("Platform", style="bold")
            table.add_column("Profile URL", overflow="fold")
            table.add_column("Status")
            table.add_column("Confidence")
            table.add_column("Evidence", overflow="fold")
            for p in items:
                table.add_row(
                    escape(p["platform"]),
                    escape(p["canonical_url"]),
                    styled(p["verification_status"]),
                    f"{styled(p['confidence_level'], CONFIDENCE_STYLE)} ({p['confidence_score']})",
                    escape(p["evidence_description"]),
                )
            self.table(table)
        if summary:
            counts = summary.get("counts", {})
            self.console.print(
                f"[bold]Summary:[/bold] {counts.get('total_profiles', len(items))} URL(s) · "
                f"[green]{counts.get('VERIFIED_PUBLIC_REFERENCE', 0)} verified[/green] · "
                f"[yellow]{counts.get('PUBLIC_CANDIDATE', 0)} candidates[/yellow] · "
                f"{counts.get('UNVERIFIED', 0)} unverified · "
                f"{counts.get('INACCESSIBLE', 0) + counts.get('RESTRICTED', 0)} inaccessible/restricted"
            )
            self.limitations(summary.get("limitations") or [])

    def verification(self, data: dict[str, Any]) -> None:
        if self.json_mode:
            self.data(data)
            return
        if self.quiet:
            self.line(
                f"{data['verification_status']}\t{data['confidence_level']}\t"
                f"{data.get('canonical_url') or data['input_url']}"
            )
            return
        header = (
            f"[bold]{escape(str(data.get('platform_name') or 'Unknown platform'))}[/bold] "
            f"{escape(str(data.get('canonical_url') or data['input_url']))}\n"
            f"Status {styled(data['verification_status'])} · confidence "
            f"{styled(data['confidence_level'], CONFIDENCE_STYLE)} ({data['confidence_score']}/100)\n"
            f"{escape(data['evidence_description'])}"
        )
        self.console.print(Panel(header, title="URL verification", expand=False))
        stages = Table(title="Verification stages")
        stages.add_column("Stage", style="bold")
        stages.add_column("Outcome")
        stages.add_column("Detail", overflow="fold")
        colour = {"passed": "green", "failed": "red", "skipped": "dim", "info": "cyan"}
        for stage in data.get("stages", []):
            c = colour.get(stage["outcome"], "white")
            stages.add_row(stage["stage"], f"[{c}]{stage['outcome']}[/{c}]", escape(stage["detail"]))
        self.table(stages)
        for factor in data.get("confidence_factors", []):
            self.console.print(f"  {factor['points']:+d}  {escape(str(factor['description']))}")
        self.limitations(data.get("limitations") or [])

    def history(self, data: dict[str, Any]) -> None:
        if self.json_mode:
            self.data(data)
            return
        items = data.get("items", [])
        if self.quiet:
            for i in items:
                self.line(f"{i['id']}\t{i['investigation_reference']}\t{i['status']}")
            return
        table = Table(title=f"Investigations ({data.get('total', 0)} total, page {data.get('page', 1)})")
        for column in ("Reference", "ID", "Number", "Basis", "Status", "Profiles", "Created"):
            table.add_column(column)
        for i in items:
            table.add_row(
                escape(i["investigation_reference"]),
                i["id"],
                escape(str(i.get("normalized_phone") or "-")),
                escape(i["authorization_basis"]),
                styled(i["status"]),
                str(i.get("profile_count", 0)),
                str(i["created_at"])[:19].replace("T", " "),
            )
        self.table(table)
