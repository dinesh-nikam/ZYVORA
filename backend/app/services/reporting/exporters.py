"""Report exporters: JSON, CSV, HTML and PDF.

* CSV cells are neutralised against spreadsheet formula injection.
* HTML is rendered with Jinja2 autoescaping and a restrictive CSP; no scripts.
* PDF uses a Unicode TTF font when one is available (PDF_FONT_PATH or DejaVu);
  otherwise it falls back to a core font with non-Latin-1 characters replaced
  and records that in the document.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from enum import StrEnum
from pathlib import Path

from fpdf import FPDF
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.schemas.report import InvestigationReport

logger = logging.getLogger(__name__)

_TEMPLATES = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(["html", "j2"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)


class ExportFormat(StrEnum):
    JSON = "json"
    CSV = "csv"
    HTML = "html"
    PDF = "pdf"


MEDIA_TYPES = {
    ExportFormat.JSON: "application/json",
    ExportFormat.CSV: "text/csv; charset=utf-8",
    ExportFormat.HTML: "text/html; charset=utf-8",
    ExportFormat.PDF: "application/pdf",
}


def export(report: InvestigationReport, fmt: ExportFormat, *, pdf_font_path: str | None = None) -> bytes:
    if fmt is ExportFormat.JSON:
        return json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False).encode("utf-8")
    if fmt is ExportFormat.CSV:
        return to_csv(report)
    if fmt is ExportFormat.HTML:
        return to_html(report).encode("utf-8")
    return to_pdf(report, font_path=pdf_font_path)


def csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r", "|", "%"):
        return "'" + text
    return text


def to_csv(report: InvestigationReport) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(
        [
            "investigation_reference",
            "platform",
            "profile_url",
            "verification_status",
            "confidence_level",
            "confidence_score",
            "discovery_source",
            "evidence_description",
            "evidence_sources",
            "verified_at",
        ]
    )
    for p in report.profiles:
        writer.writerow(
            [
                csv_safe(v)
                for v in (
                    report.investigation_reference,
                    p.platform,
                    p.canonical_url,
                    p.verification_status,
                    p.confidence_level,
                    p.confidence_score,
                    p.discovery_source,
                    p.evidence_description,
                    " | ".join(f"{e.discovery_method}:{e.source_url}" for e in p.evidence),
                    p.verified_at.isoformat() if p.verified_at else "",
                )
            ]
        )
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def to_html(report: InvestigationReport) -> str:
    return _env.get_template("report.html.j2").render(r=report)


class _ReportPDF(FPDF):
    def __init__(self, reference: str) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.reference = reference

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font(self.font_family, size=7)
        self.set_text_color(110, 110, 110)
        self.cell(0, 5, f"PHONEINTEL {self.reference} - confidential - page {self.page_no()}/{{nb}}", align="C")


def _resolve_font(font_path: str | None) -> str | None:
    for candidate in ((font_path,) if font_path else ()) + _FONT_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def to_pdf(report: InvestigationReport, *, font_path: str | None = None) -> bytes:
    pdf = _ReportPDF(report.investigation_reference)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_title(f"PHONEINTEL report {report.investigation_reference}")
    pdf.set_creator("PHONEINTEL")
    ttf = _resolve_font(font_path)
    unicode_ok = ttf is not None
    if ttf:
        pdf.add_font("Report", "", ttf)
        family = "Report"
    else:
        logger.warning("No Unicode TTF font found for PDF export; non-Latin-1 characters will be replaced")
        family = "Helvetica"

    def t(value: object) -> str:
        text = "" if value is None else str(value)
        return text if unicode_ok else text.encode("latin-1", "replace").decode("latin-1")

    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def heading(text: str) -> None:
        pdf.ln(2)
        pdf.set_font(family, size=12)
        pdf.set_text_color(20, 40, 90)
        pdf.cell(width, 7, t(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.set_font(family, size=9)

    def line(label: str, value: object) -> None:
        pdf.multi_cell(
            width, 5, t(f"{label}: {value if value not in (None, '') else '-'}"), new_x="LMARGIN", new_y="NEXT"
        )

    pdf.set_font(family, size=16)
    pdf.cell(width, 9, t("PHONEINTEL Investigation Report"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, size=9)
    line("Reference", report.investigation_reference)
    line("Status", report.investigation_status)
    line("Authorization basis", report.authorization_basis)
    line("Purpose", report.purpose)
    line("Generated", report.generated_at.isoformat())
    if not unicode_ok:
        line("Note", "Unicode font unavailable; some characters may be replaced with '?'")

    heading("Phone intelligence")
    ph = report.phone_intelligence
    line("Normalized number", ph.normalized_number)
    line("Country code", ph.country_code)
    line("Country", ph.country)
    line("Number type", ph.number_type)
    line("Validation status", ph.validation_status)
    line("Carrier (allocation)", ph.carrier)
    line("Metadata provider", ph.metadata_provider)

    heading("Investigation summary")
    s = report.summary
    line("Discovered URLs", s.discovered_urls)
    line("Verified public references", s.verified_public_references)
    line("Public candidates", s.public_candidates)
    line("Unverified", s.unverified)
    line("Inaccessible / restricted / not found", f"{s.inaccessible} / {s.restricted} / {s.not_found}")

    heading("Social profile discovery")
    if not report.profiles:
        line("Result", "No profile links were discovered from permitted public sources.")
    for p in report.profiles:
        pdf.set_font(family, size=10)
        pdf.multi_cell(width, 5, t(f"{p.platform.upper()} - {p.canonical_url}"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(family, size=8)
        line("Status", f"{p.verification_status} | confidence {p.confidence_level} ({p.confidence_score}/100)")
        line("Evidence", p.evidence_description)
        for ev in p.evidence[:6]:
            line(
                f"  - {ev.discovery_method} ({ev.source_reliability})",
                f"{ev.source_url} @ {ev.timestamp:%Y-%m-%d %H:%M}Z",
            )
        for lim in p.attribution_limitations[:3]:
            line("  Limitation", lim)
        pdf.ln(1)

    heading("Sources")
    for src in report.sources[:200]:
        line(
            f"{src.discovery_method} / {src.source_reliability}",
            f"{src.source_url} ({src.timestamp:%Y-%m-%d %H:%M}Z)",
        )

    heading("Limitations")
    for lim in s.limitations:
        pdf.multi_cell(width, 4.5, t(f"- {lim}"), new_x="LMARGIN", new_y="NEXT")
    heading("Methodology")
    for item in report.methodology:
        pdf.multi_cell(width, 4.5, t(f"- {item}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font(family, size=8)
    pdf.multi_cell(width, 4.5, t(report.disclaimer), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
