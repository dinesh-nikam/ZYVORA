"""CLI end-to-end tests against a real PHONEINTEL backend running in-process
(uvicorn thread; SQLite + fakeredis; public internet replaced by MockWeb)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from phoneintel_cli.config import CLIConfig
from phoneintel_cli.main import app
from tests.live_server import LiveBackend
from typer.testing import CliRunner

PASSWORD = "Correct-Horse-Battery-9"
AUTH_FLAGS = ["--purpose", "Fraud ticket 4411 triage", "--basis", "fraud_prevention", "--ack"]


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveBackend]:
    server = LiveBackend(tmp_path_factory.mktemp("cli-backend")).start()
    yield server
    server.stop()


@pytest.fixture
def cli(backend: LiveBackend, tmp_path: Path):  # type: ignore[no-untyped-def]
    runner = CliRunner()
    env = {
        "PHONEINTEL_HOME": str(tmp_path / "home"),
        "PHONEINTEL_API_URL": backend.base_url,
        "COLUMNS": "220",
        "NO_COLOR": "1",
        "PHONEINTEL_API_KEY": "",
    }

    def invoke(*args: str, input: str | None = None):  # type: ignore[no-untyped-def]
        return runner.invoke(app, list(args), env=env, input=input)

    invoke.env = env  # type: ignore[attr-defined]
    return invoke


@pytest.fixture
def logged_in(cli, tmp_path: Path):  # type: ignore[no-untyped-def]
    email = f"cli-{tmp_path.name[-12:].lower().replace('_', '')}@example.org"
    assert cli("register", "--email", email, "--password", PASSWORD).exit_code == 0
    result = cli("login", "--email", email, "--password", PASSWORD)
    assert result.exit_code == 0, result.output
    return cli


def test_help_and_version(cli) -> None:  # type: ignore[no-untyped-def]
    result = cli("--help")
    assert result.exit_code == 0
    for command in (
        "validate",
        "country",
        "carrier",
        "profile-links",
        "public-search",
        "verify-url",
        "report",
        "history",
        "delete-data",
        "login",
        "config",
    ):
        assert command in result.output
    assert cli("--version").output.strip() == "phoneintel 1.0.0"


def test_unauthenticated_exit_code(cli) -> None:  # type: ignore[no-untyped-def]
    result = cli("validate", "+919876543210")
    assert result.exit_code == 3
    assert "login" in result.output


def test_login_stores_session_and_config_masks_secrets(logged_in, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    stored = json.loads((tmp_path / "home" / "config.json").read_text())
    assert stored["access_token"] and stored["refresh_token"]
    shown = logged_in("config", "show")
    assert shown.exit_code == 0
    assert stored["access_token"] not in shown.output
    assert stored["refresh_token"] not in shown.output
    assert logged_in("-q", "whoami").output.strip().endswith("@example.org")


def test_phone_commands(logged_in) -> None:  # type: ignore[no-untyped-def]
    result = logged_in("validate", "+91 98765 43210")
    assert result.exit_code == 0
    assert "+919876543210" in result.output and "VALID" in result.output
    as_json = json.loads(logged_in("--json", "validate", "+919876543210").stdout)
    assert as_json["country_code"] == 91
    assert logged_in("validate", "+12345").exit_code == 1  # parseable but invalid
    assert logged_in("validate", "not-a-number").exit_code == 6  # rejected input
    country = logged_in("country", "098765 43210", "--region", "IN")
    assert country.exit_code == 0 and "India" in country.output
    carrier = logged_in("--json", "carrier", "+919876543210")
    assert json.loads(carrier.stdout)["carrier_source"].startswith("libphonenumber")


def test_profile_links_requires_explicit_authorization(logged_in) -> None:  # type: ignore[no-untyped-def]
    missing_basis = logged_in("profile-links", "+919876543210", "--purpose", "Fraud ticket 4411", "--ack")
    assert missing_basis.exit_code == 2
    assert "--basis" in missing_basis.output
    missing_ack = logged_in(
        "profile-links", "+919876543210", "--purpose", "Fraud ticket 4411", "--basis", "fraud_prevention"
    )
    assert missing_ack.exit_code == 2
    bad_basis = logged_in("profile-links", "+919876543210", *AUTH_FLAGS[:2], "--basis", "curiosity", "--ack")
    assert bad_basis.exit_code == 2
    # Nothing was created by the refused attempts.
    assert json.loads(logged_in("--json", "history").stdout)["total"] == 0


def test_profile_links_full_flow_and_reports(logged_in, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    result = logged_in(
        "profile-links",
        "+919876543210",
        *AUTH_FLAGS,
        "--platform",
        "telegram",
        "--platform",
        "instagram",
        "--platform",
        "github",
        "--url",
        "https://github.com/ghost-user",
    )
    assert result.exit_code == 0, result.output
    assert "https://t.me/acmehelp" in result.output
    assert "VERIFIED_PUBLIC_REFERENCE" in result.output
    assert "NOT_FOUND" in result.output

    status = json.loads(logged_in("--json", "status", "--profiles").stdout)
    assert status["investigation"]["status"] == "completed"
    urls = {p["canonical_url"]: p["verification_status"] for p in status["profiles"]}
    assert urls["https://www.instagram.com/acme_support/"] == "PUBLIC_CANDIDATE"

    report_json = json.loads(logged_in("report").stdout)  # defaults to the last investigation
    assert report_json["summary"]["verified_public_references"] == 2
    pdf_path = tmp_path / "out.pdf"
    pdf = logged_in("report", "--format", "pdf", "--output", str(pdf_path))
    assert pdf.exit_code == 0 and pdf_path.read_bytes().startswith(b"%PDF")
    csv_out = logged_in("report", "--format", "csv")
    assert "investigation_reference" in csv_out.stdout
    html_path = tmp_path / "r.html"
    assert logged_in("report", "-f", "html", "-o", str(html_path)).exit_code == 0
    assert "<!doctype html>" in html_path.read_text(encoding="utf-8")

    hist = json.loads(logged_in("--json", "history", "--status", "completed").stdout)
    assert hist["total"] == 1
    quiet = logged_in("-q", "history").stdout.strip().splitlines()
    assert len(quiet) == 1 and quiet[0].count("\t") == 2


def test_public_search_no_wait_and_cancel_semantics(logged_in) -> None:  # type: ignore[no-untyped-def]
    queued = logged_in("--json", "public-search", "+919876543210", *AUTH_FLAGS, "--no-wait")
    assert queued.exit_code == 0, queued.output
    payload = json.loads(queued.stdout)
    assert payload["job"]["status"] == "queued"
    inv_id = payload["investigation"]["id"]
    # Either it is still running (cancel accepted) or already finished (409 -> exit code 8).
    assert logged_in("cancel", "--id", inv_id).exit_code in (0, 8)


def test_verify_url(logged_in) -> None:  # type: ignore[no-untyped-def]
    ok = logged_in("verify-url", "https://t.me/acmehelp", "--phone", "+919876543210")
    assert ok.exit_code == 0
    assert "VERIFIED_PUBLIC_REFERENCE" in ok.output and "url_syntax" in ok.output
    blocked = logged_in("--json", "verify-url", "http://169.254.169.254/latest/meta-data")
    assert blocked.exit_code == 1
    assert json.loads(blocked.stdout)["verification_status"] == "ERROR"


def test_delete_data(logged_in) -> None:  # type: ignore[no-untyped-def]
    created = json.loads(logged_in("--json", "profile-links", "+919876543210", *AUTH_FLAGS, "--no-wait").stdout)
    inv_id = created["investigation"]["id"]
    assert logged_in("delete-data", inv_id).exit_code == 2  # no confirmation in non-interactive mode
    assert logged_in("delete-data").exit_code == 2  # needs id or --all
    assert logged_in("delete-data", inv_id, "--yes").exit_code == 0
    assert logged_in("status", "--id", inv_id).exit_code == 5
    erased = json.loads(logged_in("--json", "delete-data", "--all", "--yes").stdout)
    assert "investigations_erased" in erased


def test_api_key_flow(logged_in) -> None:  # type: ignore[no-untyped-def]
    created = json.loads(logged_in("--json", "apikey", "create", "ci-key", "-p", "phone:validate").stdout)
    key = created["api_key"]
    assert key.startswith("pi_")
    assert key not in logged_in("apikey", "list").output
    env_key = logged_in("--api-key", key, "--json", "validate", "+919876543210")
    assert env_key.exit_code == 0
    denied = logged_in("--api-key", key, "history")
    assert denied.exit_code == 4


def test_expired_access_token_is_refreshed(logged_in, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "home" / "config.json"
    data = json.loads(path.read_text())
    old_refresh = data["refresh_token"]
    data["access_token"] = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.invalid-signature"
    path.write_text(json.dumps(data))
    result = logged_in("whoami")
    assert result.exit_code == 0, result.output
    rotated = json.loads(path.read_text())
    assert rotated["refresh_token"] != old_refresh


def test_logout(logged_in) -> None:  # type: ignore[no-untyped-def]
    assert logged_in("logout").exit_code == 0
    assert logged_in("whoami").exit_code == 3


def test_config_set_validation(cli) -> None:  # type: ignore[no-untyped-def]
    assert cli("config", "set", "timeout_seconds", "abc").exit_code == 2
    assert cli("config", "set", "access_token", "x").exit_code == 2
    assert cli("config", "set", "timeout_seconds", "45").exit_code == 0
    assert CLIConfig.path().name == "config.json"
