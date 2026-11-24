import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gtm_engine.adapters.millionverifier import LiveMillionVerifierAdapter
from gtm_engine.adapters.mock_verifier import MockEmailVerifierAdapter
from gtm_engine.adapters.waterfall_verifier import VerificationWaterfall
from gtm_engine.cli.verify_leads import load_leads, main, run_verification
from gtm_engine.contracts.leads import LeadInput, LeadVerificationResult, VerificationStatus

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_LEADS = ROOT / "fixtures" / "sample_leads.json"
FAKE_API_KEY = "mv-test-key-not-real"
WEBHOOK_URL = "https://hooks.example.test/verification"


class RecordingVerifier:
    """Test double that returns a fixed status and records every email it was asked to verify."""

    def __init__(self, provider_name: str, status: VerificationStatus) -> None:
        self.provider_name = provider_name
        self.status = status
        self.calls: list[str] = []

    def verify_email(self, email: str) -> tuple[VerificationStatus, list[str]]:
        self.calls.append(email)
        return self.status, [f"said_{self.status}"]


class FailingVerifier:
    provider_name = "flaky"

    def verify_email(self, email: str) -> tuple[VerificationStatus, list[str]]:
        raise RuntimeError("Failed to reach Flaky API: timed out")


def _mv_response(payload: dict, status: int = 200) -> MagicMock:
    cm = MagicMock()
    resp = cm.__enter__.return_value
    resp.status = status
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    return cm


# --- 1. Waterfall stops after provider 1 on a clear answer (credit savings) ---------------


@pytest.mark.parametrize("primary_status", ["valid", "invalid"])
def test_waterfall_stops_after_primary_on_clear_result(primary_status: VerificationStatus) -> None:
    primary = RecordingVerifier("primary", primary_status)
    fallback = RecordingVerifier("fallback", "valid")

    result = VerificationWaterfall([primary, fallback]).verify("Lead@Example.com")

    assert primary.calls == ["lead@example.com"]
    assert fallback.calls == []  # provider 2 never called: no credits spent
    assert result.status == primary_status
    assert result.provider == "primary"
    assert result.attempts == 1
    assert result.deliverable is (primary_status == "valid")


def test_waterfall_skips_all_providers_for_bad_syntax() -> None:
    primary = RecordingVerifier("primary", "valid")

    result = VerificationWaterfall([primary]).verify("not-an-email")

    assert primary.calls == []
    assert result.status == "invalid"
    assert result.provider == "syntax_check"
    assert result.attempts == 0
    assert result.reasons == ["invalid_syntax"]


# --- 2. Waterfall escalates to provider 2 on risky / unknown -------------------------------


@pytest.mark.parametrize("primary_status", ["unknown", "risky"])
@pytest.mark.parametrize("fallback_status", ["valid", "invalid"])
def test_waterfall_escalates_to_fallback_when_primary_is_unsure(
    primary_status: VerificationStatus, fallback_status: VerificationStatus
) -> None:
    primary = RecordingVerifier("primary", primary_status)
    fallback = RecordingVerifier("fallback", fallback_status)

    result = VerificationWaterfall([primary, fallback]).verify("lead@example.com")

    assert primary.calls == ["lead@example.com"]
    assert fallback.calls == ["lead@example.com"]
    assert result.status == fallback_status
    assert result.provider == "fallback"
    assert result.attempts == 2
    assert result.reasons == [f"primary:said_{primary_status}", f"fallback:said_{fallback_status}"]


def test_later_unknown_does_not_override_earlier_risky() -> None:
    primary = RecordingVerifier("primary", "risky")
    fallback = RecordingVerifier("fallback", "unknown")

    result = VerificationWaterfall([primary, fallback]).verify("lead@example.com")

    assert result.status == "risky"
    assert result.provider == "primary"
    assert result.attempts == 2


def test_provider_error_falls_back_to_next_provider() -> None:
    fallback = RecordingVerifier("fallback", "valid")

    result = VerificationWaterfall([FailingVerifier(), fallback]).verify("lead@example.com")

    assert result.status == "valid"
    assert result.provider == "fallback"
    assert result.attempts == 2
    assert result.reasons[0] == "flaky:provider_error:Failed to reach Flaky API: timed out"


def test_waterfall_requires_at_least_one_provider() -> None:
    with pytest.raises(ValueError):
        VerificationWaterfall([])


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("jane@acme.com", "valid"),
        ("invalid.person@acme.com", "invalid"),
        ("bounce@acme.com", "invalid"),
        ("ops@catchall-agency.com", "risky"),
        ("risky.lead@acme.com", "risky"),
        ("unknown@acme.com", "unknown"),
        ("no-at-sign.acme.com", "invalid"),
    ],
)
def test_mock_verifier_is_deterministic(email: str, expected: VerificationStatus) -> None:
    status, reasons = MockEmailVerifierAdapter().verify_email(email)
    assert status == expected
    assert reasons


# --- 3. MillionVerifier mapping and API-key safety -----------------------------------------


@pytest.mark.parametrize(
    ("mv_result", "expected"),
    [
        ("ok", "valid"),
        ("catch_all", "risky"),
        ("unknown", "risky"),
        ("invalid", "invalid"),
        ("disposable", "invalid"),
        ("something_new", "unknown"),
    ],
)
@patch("urllib.request.urlopen")
def test_millionverifier_maps_results(
    mock_urlopen: MagicMock, mv_result: str, expected: VerificationStatus
) -> None:
    mock_urlopen.return_value = _mv_response({"email": "a+tag@acme.com", "result": mv_result, "subresult": "x"})

    status, reasons = LiveMillionVerifierAdapter(FAKE_API_KEY).verify_email("a+tag@acme.com")

    assert status == expected
    assert reasons == [f"result:{mv_result}", "subresult:x"]

    req = mock_urlopen.call_args.args[0]
    assert req.get_method() == "GET"
    assert req.full_url.startswith("https://api.millionverifier.com/api/v3/?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
    assert query == {"api": [FAKE_API_KEY], "email": ["a+tag@acme.com"]}


@patch("urllib.request.urlopen")
def test_millionverifier_http_error_does_not_leak_key(mock_urlopen: MagicMock) -> None:
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url=f"https://api.millionverifier.com/api/v3/?api={FAKE_API_KEY}&email=a@acme.com",
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )

    with pytest.raises(RuntimeError) as exc_info:
        LiveMillionVerifierAdapter(FAKE_API_KEY).verify_email("a@acme.com")

    assert "401" in str(exc_info.value)
    assert FAKE_API_KEY not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@patch("urllib.request.urlopen")
def test_millionverifier_api_error_field_is_redacted(mock_urlopen: MagicMock) -> None:
    mock_urlopen.return_value = _mv_response({"error": f"Invalid API key {FAKE_API_KEY}"})

    with pytest.raises(RuntimeError) as exc_info:
        LiveMillionVerifierAdapter(FAKE_API_KEY).verify_email("a@acme.com")

    assert "Invalid API key ***" in str(exc_info.value)
    assert FAKE_API_KEY not in str(exc_info.value)


@patch("urllib.request.urlopen")
def test_millionverifier_unreachable_does_not_leak_key(mock_urlopen: MagicMock) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")

    with pytest.raises(RuntimeError) as exc_info:
        LiveMillionVerifierAdapter(FAKE_API_KEY).verify_email("a@acme.com")

    assert "Failed to reach MillionVerifier API" in str(exc_info.value)
    assert FAKE_API_KEY not in str(exc_info.value)


def test_millionverifier_rejects_blank_key() -> None:
    with pytest.raises(ValueError):
        LiveMillionVerifierAdapter("  ")


# --- 4. Contracts, batch report counts and CLI exit codes ----------------------------------


def test_lead_input_normalizes_email_and_deliverable_follows_status() -> None:
    assert LeadInput(email="  Jane.Doe@ACME.com ").email == "jane.doe@acme.com"
    assert LeadVerificationResult(email="a@b.co", status="valid", provider="p", attempts=1).deliverable is True
    # deliverable is derived from status, even if a caller passes the wrong value
    risky = LeadVerificationResult(email="a@b.co", status="risky", deliverable=True, provider="p", attempts=1)
    assert risky.deliverable is False


def test_batch_report_counts_and_duplicate_leads_verified_once() -> None:
    primary = RecordingVerifier("primary", "valid")
    leads = [
        LeadInput(email="a@acme.com"),
        LeadInput(email="A@ACME.com "),  # duplicate after normalization
        LeadInput(email="b@acme.com"),
    ]

    report = run_verification(leads, VerificationWaterfall([primary]))

    assert primary.calls == ["a@acme.com", "b@acme.com"]
    assert report.total_leads == 3
    assert report.valid_count == 3
    assert report.deliverable_emails == ["a@acme.com", "b@acme.com"]


def test_batch_report_counts_from_sample_fixture() -> None:
    waterfall = VerificationWaterfall(
        [MockEmailVerifierAdapter("mock_primary"), MockEmailVerifierAdapter("mock_fallback")]
    )

    report = run_verification(load_leads(SAMPLE_LEADS), waterfall)

    assert report.total_leads == 7
    assert report.valid_count == 3
    assert report.risky_count == 1
    assert report.invalid_count == 2
    assert report.unknown_count == 1
    assert report.deliverable_emails == ["jane.doe@healthy-outreach.com", "sam.lee@healthy-outreach.com"]


def _run_cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("VERIFIER_API_KEY", raising=False)
    monkeypatch.delenv("VERIFICATION_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["verify_leads", *args])
    return main()


def test_cli_exit_codes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_cli(monkeypatch) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["invalid_count"] == 2

    assert _run_cli(monkeypatch, "--strict") == 1
    capsys.readouterr()

    clean = tmp_path / "clean_leads.json"
    clean.write_text(json.dumps([{"email": "jane@acme.com"}, {"email": "sam@acme.com"}]), encoding="utf-8")
    assert _run_cli(monkeypatch, "--leads-file", str(clean), "--strict") == 0
    capsys.readouterr()

    broken = tmp_path / "broken_leads.json"
    broken.write_text(json.dumps({"email": "not-a-list@acme.com"}), encoding="utf-8")
    assert _run_cli(monkeypatch, "--leads-file", str(broken)) == 2
    assert "could not load leads" in capsys.readouterr().err


def test_cli_live_mode_requires_api_key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--mode", "live")
    assert exc_info.value.code == 2
    assert "requires --verifier-api-key" in capsys.readouterr().err


@patch("urllib.request.urlopen")
def test_cli_posts_report_to_webhook(
    mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_urlopen.return_value.__enter__.return_value.status = 200

    assert _run_cli(monkeypatch, "--webhook-url", WEBHOOK_URL) == 0

    printed = json.loads(capsys.readouterr().out)
    req = mock_urlopen.call_args.args[0]
    assert req.full_url == WEBHOOK_URL
    assert req.get_method() == "POST"
    assert json.loads(req.data.decode("utf-8")) == printed


@patch("urllib.request.urlopen")
def test_cli_prints_report_before_webhook_failure(
    mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")

    assert _run_cli(monkeypatch, "--webhook-url", WEBHOOK_URL) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out)["total_leads"] == 7
    assert "Failed to deliver verification webhook" in captured.err
