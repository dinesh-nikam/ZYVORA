from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

from app.schemas.report import (
    InvestigationReport,
    ReportEvidence,
    ReportPhone,
    ReportProfile,
    ReportSource,
    ReportSummary,
)
from app.services.reporting.exporters import ExportFormat, csv_safe, export

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def sample(purpose: str = "Fraud triage", url: str = "https://t.me/acmehelp") -> InvestigationReport:
    evidence = ReportEvidence(
        source_url=url,
        discovery_method="live_public_page",
        source_name="telegram_public_page",
        source_reliability="high",
        summary="Call [investigated number] - Ünïcödé ✓",
        phone_reference_found=True,
        timestamp=NOW,
    )
    return InvestigationReport(
        generated_at=NOW,
        investigation_reference="PI-20261114-ABC123",
        investigation_status="completed",
        authorization_basis="fraud_prevention",
        purpose=purpose,
        created_at=NOW,
        completed_at=NOW,
        phone_intelligence=ReportPhone(
            normalized_number="+919876543210",
            country_code=91,
            country="India",
            region_code="IN",
            number_type="MOBILE",
            validation_status="VALID",
            carrier="Vodafone",
            metadata_provider="libphonenumber",
            checked_at=NOW,
        ),
        profiles=[
            ReportProfile(
                platform="telegram",
                profile_url=url,
                canonical_url=url,
                discovery_source="search_index",
                evidence_description="published",
                verification_status="VERIFIED_PUBLIC_REFERENCE",
                confidence_level="HIGH",
                confidence_score=75,
                confidence_factors=[{"factor": "phone_on_profile_live", "points": 65, "description": "d"}],
                attribution_limitations=["Does not prove control"],
                verified_at=NOW,
                evidence=[evidence],
            )
        ],
        sources=[
            ReportSource(
                source_url=url,
                discovery_method="live_public_page",
                source_name="t",
                timestamp=NOW,
                source_reliability="high",
            )
        ],
        summary=ReportSummary(
            discovered_urls=1,
            verified_public_references=1,
            public_candidates=0,
            unverified=0,
            inaccessible=0,
            not_found=0,
            restricted=0,
            errors=0,
            limitations=["limit"],
        ),
        methodology=["m"],
        disclaimer="disclaimer",
    )


def test_json_export() -> None:
    data = json.loads(export(sample(), ExportFormat.JSON))
    assert data["investigation_reference"] == "PI-20261114-ABC123"
    assert data["profiles"][0]["verification_status"] == "VERIFIED_PUBLIC_REFERENCE"
    assert data["summary"]["verified_public_references"] == 1


def test_csv_export_and_formula_injection_guard() -> None:
    raw = export(sample(url="https://t.me/acmehelp"), ExportFormat.CSV).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw)))
    assert rows[0][0] == "investigation_reference"
    assert rows[1][2] == "https://t.me/acmehelp"
    for payload in ('=HYPERLINK("x")', "+cmd", "-1+1", "@SUM(A1)"):
        assert csv_safe(payload).startswith("'")


def test_html_export_escapes_untrusted_content() -> None:
    html = export(sample(purpose='<script>alert("x")</script><img src=x onerror=1>'), ExportFormat.HTML).decode()
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "onerror=1>" not in html
    assert "Content-Security-Policy" in html


def test_pdf_export() -> None:
    pdf = export(sample(), ExportFormat.PDF)
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1000
