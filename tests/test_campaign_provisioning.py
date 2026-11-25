import json
import re
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from gtm_engine.adapters.mock_campaign import MockCampaignProvisionerAdapter
from gtm_engine.adapters.smartlead_campaign import CampaignProvisioningError, LiveSmartleadCampaignAdapter
from gtm_engine.adapters.smartlead_client import SmartleadApiError, SmartleadAuthError
from gtm_engine.cli.provision_campaign import build_provisioning_request, main
from gtm_engine.contracts.campaign import CampaignProvisioningRequest, ProvisioningGuardrailError
from gtm_engine.contracts.leads import LeadVerificationResult, VerificationBatchReport
from gtm_engine.contracts.readiness import ReadinessBatchReport

ROOT = Path(__file__).resolve().parent.parent
READINESS_FIXTURE = ROOT / "fixtures" / "sample_readiness_report.json"
VERIFICATION_FIXTURE = ROOT / "fixtures" / "sample_verification_report.json"
FAKE_API_KEY = "sl-test-key-not-real"
WEBHOOK_URL = "https://hooks.example.test/campaign"


# --- helpers --------------------------------------------------------------------------------


def _lead(email: str, status: str = "valid", first_name: str | None = "Pat") -> LeadVerificationResult:
    return LeadVerificationResult(
        email=email,
        first_name=first_name,
        last_name="Lee",
        company_domain=email.rsplit("@", 1)[-1],
        status=status,  # type: ignore[arg-type]
        provider="mock",
        attempts=1,
    )


def _account(account_id: int, email: str, status: str = "ACTIVE", blocked: bool = False, smtp_ok: bool = True) -> dict:
    return {
        "id": account_id,
        "from_email": email,
        "is_smtp_success": smtp_ok,
        "warmup_details": {"status": status, "is_warmup_blocked": blocked},
    }


def _request(**overrides: Any) -> CampaignProvisioningRequest:
    fields: dict[str, Any] = {
        "campaign_name": "Q1 Outbound",
        "allowed_domains": ["ready.com"],
        "domain_daily_caps": {"ready.com": 30},
        "leads": [_lead("a@prospect.com")],
    }
    fields.update(overrides)
    return CampaignProvisioningRequest(**fields)


class FakeSmartlead:
    """Stands in for urllib.request.urlopen: routes Smartlead API calls and records them in order."""

    def __init__(
        self,
        accounts: list[dict] | None = None,
        campaign_status: str = "PAUSED",
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.accounts = accounts if accounts is not None else [_account(11, "inbox@ready.com")]
        self.campaign_status = campaign_status
        self.errors = errors or {}
        self.calls: list[tuple[str, str, dict[str, list[str]], Any]] = []

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [(m, p, b) for m, p, _, b in self.calls if m != "GET"]

    def _response(self, payload: Any) -> MagicMock:
        cm = MagicMock()
        resp = cm.__enter__.return_value
        resp.status = 200
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        return cm

    def __call__(self, req: Any, timeout: float | None = None) -> MagicMock:
        parts = urllib.parse.urlsplit(req.full_url)
        path = parts.path.removeprefix("/api/v1")
        query = urllib.parse.parse_qs(parts.query)
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        method = req.get_method()
        self.calls.append((method, path, query, body))

        route = re.sub(r"/campaigns/\d+", "/campaigns/{id}", path)
        key = f"{method} {route}"
        if key in self.errors:
            raise self.errors[key]

        if key == "GET /email-accounts":
            offset, limit = int(query["offset"][0]), int(query["limit"][0])
            return self._response(self.accounts[offset : offset + limit])
        if key == "POST /campaigns/create":
            return self._response({"ok": True, "id": 4242, "name": body["name"]})
        if key == "GET /campaigns/{id}":
            return self._response({"id": int(path.rsplit("/", 1)[-1]), "status": self.campaign_status})
        if key == "POST /campaigns/{id}/leads":
            return self._response({"ok": True, "upload_count": len(body["lead_list"])})
        if key in ("POST /campaigns/{id}/email-accounts", "POST /campaigns/{id}/status"):
            return self._response({"ok": True})
        raise AssertionError(f"unexpected Smartlead call: {key}")


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=f"https://server.smartlead.ai/api/v1/campaigns/create?api_key={FAKE_API_KEY}",
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


# --- 1. Guardrails block before any write call ----------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"allowed_domains": [], "domain_daily_caps": {}}, "no_ready_domains_available"),
        ({"leads": [_lead("a@x.com", "risky"), _lead("b@x.com", "invalid"), _lead("c@x.com", "unknown")]},
         "no_valid_leads_to_upload"),
        ({"leads": []}, "no_valid_leads_to_upload"),
    ],
    ids=["no_ready_domains", "no_valid_leads", "no_leads"],
)
def test_guardrails_block_with_zero_api_calls(overrides: dict, code: str) -> None:
    fake = FakeSmartlead()
    with patch("urllib.request.urlopen", side_effect=fake):
        with pytest.raises(ProvisioningGuardrailError) as exc_info:
            LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(_request(**overrides))

    assert exc_info.value.code == code
    assert str(exc_info.value) == code
    assert isinstance(exc_info.value, ValueError)
    assert fake.calls == []  # not even a read


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"allowed_domains": []}, "no_ready_domains_available"),
        ({"leads": [_lead("a@x.com", "risky")]}, "no_valid_leads_to_upload"),
    ],
)
def test_mock_guardrails_block_before_any_write(overrides: dict, code: str) -> None:
    mock = MockCampaignProvisionerAdapter()
    with pytest.raises(ProvisioningGuardrailError, match=code):
        mock.provision_campaign(_request(**overrides))
    assert mock.write_calls == []


def test_no_healthy_mailboxes_blocks_after_reads_only() -> None:
    fake = FakeSmartlead(accounts=[_account(1, "a@ready.com", blocked=True), _account(2, "b@ready.com", smtp_ok=False)])
    with patch("urllib.request.urlopen", side_effect=fake):
        with pytest.raises(ProvisioningGuardrailError, match="no_healthy_mailboxes_on_ready_domains"):
            LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(_request())

    assert [m for m, *_ in fake.calls] == ["GET"]
    assert fake.writes == []


def test_existing_active_campaign_requires_activate_flag() -> None:
    fake = FakeSmartlead(campaign_status="ACTIVE")
    with patch("urllib.request.urlopen", side_effect=fake):
        with pytest.raises(ProvisioningGuardrailError, match="campaign_already_active"):
            LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(_request(campaign_id=77))

    assert [(m, p) for m, p, *_ in fake.calls] == [("GET", "/email-accounts"), ("GET", "/campaigns/77")]
    assert fake.writes == []


# --- 2. Only valid leads are uploaded -------------------------------------------------------


def test_risky_invalid_unknown_and_duplicate_leads_are_stripped() -> None:
    leads = [
        _lead("good@prospect.com"),
        _lead("risky@catchall.com", "risky"),
        _lead("bad@prospect.com", "invalid"),
        _lead("unsure@prospect.com", "unknown"),
        _lead("good@prospect.com"),
        _lead("also.good@prospect.com", first_name=None),
    ]
    fake = FakeSmartlead()
    with patch("urllib.request.urlopen", side_effect=fake):
        report = LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(_request(leads=leads))

    uploaded = [lead for m, p, _, b in fake.calls if p.endswith("/leads") for lead in b["lead_list"]]
    assert [lead["email"] for lead in uploaded] == ["good@prospect.com", "also.good@prospect.com"]
    assert uploaded[0] == {"email": "good@prospect.com", "first_name": "Pat", "last_name": "Lee", "website": "prospect.com"}
    assert "first_name" not in uploaded[1]
    assert report.uploaded_leads_count == 2
    assert report.skipped_leads_count == 4
    assert "skipped_non_valid_leads:3" in report.reasons
    assert "skipped_duplicate_leads:1" in report.reasons


def test_mock_strips_non_valid_leads_from_day2_fixture() -> None:
    verification = VerificationBatchReport.model_validate_json(VERIFICATION_FIXTURE.read_text(encoding="utf-8"))
    request = build_provisioning_request(
        "Q1", ReadinessBatchReport.model_validate_json(READINESS_FIXTURE.read_text(encoding="utf-8")), verification
    )

    report = MockCampaignProvisionerAdapter().provision_campaign(request)

    assert verification.total_leads == 7
    assert report.uploaded_leads_count == 2  # jane + sam; the duplicate jane and 4 non-valid leads skipped
    assert report.skipped_leads_count == 5


# --- 3. Only mailboxes on ready domains are attached ----------------------------------------


def test_build_request_excludes_blocked_domains_from_day1_fixture() -> None:
    readiness = ReadinessBatchReport.model_validate_json(READINESS_FIXTURE.read_text(encoding="utf-8"))
    verification = VerificationBatchReport.model_validate_json(VERIFICATION_FIXTURE.read_text(encoding="utf-8"))

    request = build_provisioning_request("Q1", readiness, verification)

    assert request.allowed_domains == ["healthy-outreach.com"]
    assert request.excluded_domains == ["throttled-sender.com", "quarantined-sender.com", "broken-sender.com"]
    assert request.domain_daily_caps == {"healthy-outreach.com": 30}
    assert request.activate_campaign is False


def test_only_healthy_mailboxes_on_ready_domains_are_attached() -> None:
    accounts = [
        _account(1, "a@healthy-outreach.com"),
        _account(2, "b@Healthy-Outreach.com"),
        _account(3, "c@healthy-outreach.com", blocked=True),
        _account(4, "d@healthy-outreach.com", status="PAUSED"),
        _account(5, "e@quarantined-sender.com"),
        _account(6, "f@throttled-sender.com"),
        _account(7, "g@unrelated.com"),
    ]
    readiness = ReadinessBatchReport.model_validate_json(READINESS_FIXTURE.read_text(encoding="utf-8"))
    verification = VerificationBatchReport.model_validate_json(VERIFICATION_FIXTURE.read_text(encoding="utf-8"))
    request = build_provisioning_request("Q1", readiness, verification)
    fake = FakeSmartlead(accounts=accounts)

    with patch("urllib.request.urlopen", side_effect=fake):
        report = LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(request)

    attach = [b for m, p, _, b in fake.calls if p.endswith("/email-accounts") and m == "POST"]
    assert attach == [{"email_account_ids": [1, 2]}]
    assert report.attached_account_ids == [1, 2]
    assert report.attached_domains == ["healthy-outreach.com"]
    assert "quarantined-sender.com" in report.excluded_domains
    assert "skipped_unhealthy_mailboxes:2" in report.reasons
    assert report.total_daily_capacity == 30


def test_ready_domain_without_healthy_mailbox_is_reported_excluded() -> None:
    fake = FakeSmartlead(accounts=[_account(1, "a@ready.com"), _account(2, "b@second.com", blocked=True)])
    request = _request(allowed_domains=["ready.com", "second.com"], domain_daily_caps={"ready.com": 30, "second.com": 15})

    with patch("urllib.request.urlopen", side_effect=fake):
        report = LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(request)

    assert report.attached_domains == ["ready.com"]
    assert report.excluded_domains == ["second.com"]
    assert "no_healthy_mailboxes:second.com" in report.reasons
    assert report.total_daily_capacity == 30  # second.com's cap is not counted


# --- 4. Live call sequence, batching, draft safety and key masking -------------------------


def test_live_call_sequence_batches_and_stays_drafted() -> None:
    leads = [_lead(f"lead{i}@prospect.com") for i in range(250)]
    fake = FakeSmartlead()

    with patch("urllib.request.urlopen", side_effect=fake):
        report = LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(_request(leads=leads))

    assert [(m, p) for m, p, *_ in fake.calls] == [
        ("GET", "/email-accounts"),
        ("POST", "/campaigns/create"),
        ("POST", "/campaigns/4242/email-accounts"),
        ("POST", "/campaigns/4242/leads"),
        ("POST", "/campaigns/4242/leads"),
        ("POST", "/campaigns/4242/leads"),
    ]
    assert fake.calls[1][3] == {"name": "Q1 Outbound"}
    batches = [b for m, p, _, b in fake.calls if p.endswith("/leads")]
    assert [len(b["lead_list"]) for b in batches] == [100, 100, 50]
    assert all(
        b["settings"] == {
            "ignore_global_block_list": False,
            "ignore_unsubscribe_list": False,
            "ignore_community_bounce_list": False,
        }
        for b in batches
    )
    assert all(q["api_key"] == [FAKE_API_KEY] for _, _, q, _ in fake.calls)
    assert not any(p.endswith("/status") for _, p, *_ in fake.calls)  # never started
    assert report.campaign_id == 4242
    assert report.campaign_status == "DRAFTED"
    assert report.uploaded_leads_count == 250
    assert "campaign_left_drafted:pass --activate to start sending" in report.reasons


def test_activate_starts_campaign_last() -> None:
    fake = FakeSmartlead()
    with patch("urllib.request.urlopen", side_effect=fake):
        report = LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(_request(activate_campaign=True))

    assert fake.calls[-1][:2] == ("POST", "/campaigns/4242/status")
    assert fake.calls[-1][3] == {"status": "START"}
    assert report.campaign_status == "ACTIVE"


def test_existing_campaign_is_reused_not_created() -> None:
    fake = FakeSmartlead(campaign_status="PAUSED")
    with patch("urllib.request.urlopen", side_effect=fake):
        report = LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(_request(campaign_id=77))

    paths = [p for _, p, *_ in fake.calls]
    assert "/campaigns/create" not in paths
    assert paths[1:] == ["/campaigns/77", "/campaigns/77/email-accounts", "/campaigns/77/leads"]
    assert report.campaign_id == 77
    assert report.campaign_status == "PAUSED"


@pytest.mark.parametrize("code", [401, 403])
def test_auth_error_on_create_does_not_leak_key(code: int) -> None:
    fake = FakeSmartlead(errors={"POST /campaigns/create": _http_error(code)})
    with patch("urllib.request.urlopen", side_effect=fake):
        with pytest.raises(SmartleadAuthError) as exc_info:
            LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(_request())

    message = str(exc_info.value)
    assert f"status {code}" in message
    assert "POST /campaigns/create" in message
    assert FAKE_API_KEY not in message
    assert "api_key" not in message
    assert exc_info.value.__cause__ is None


def test_failure_after_create_reports_partial_progress_without_key() -> None:
    fake = FakeSmartlead(errors={"POST /campaigns/{id}/leads": _http_error(500)})
    with patch("urllib.request.urlopen", side_effect=fake):
        with pytest.raises(CampaignProvisioningError) as exc_info:
            LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(_request())

    message = str(exc_info.value)
    assert "status 500" in message
    assert "while uploading leads" in message
    assert "campaign 4242 status DRAFTED" in message
    assert "0 of 1 leads uploaded" in message
    assert FAKE_API_KEY not in message


def test_network_error_redacts_key() -> None:
    fake = FakeSmartlead(errors={"GET /email-accounts": urllib.error.URLError(f"bad host api_key={FAKE_API_KEY}")})
    with patch("urllib.request.urlopen", side_effect=fake):
        with pytest.raises(SmartleadApiError) as exc_info:
            LiveSmartleadCampaignAdapter(FAKE_API_KEY).provision_campaign(_request())

    assert FAKE_API_KEY not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_mock_adapter_report_and_activation() -> None:
    mock = MockCampaignProvisionerAdapter()
    report = mock.provision_campaign(_request(activate_campaign=True))

    assert report.provider == "mock"
    assert report.campaign_id == 9001
    assert report.attached_account_ids == [1001]
    assert report.campaign_status == "ACTIVE"
    assert mock.write_calls == ["create_campaign", "attach_email_accounts", "upload_leads", "start_campaign"]


def test_request_validation_normalizes_and_rejects_blank_name() -> None:
    request = _request(allowed_domains=[" Ready.COM ", "ready.com", ""], domain_daily_caps={" READY.com": 30})
    assert request.allowed_domains == ["ready.com"]
    assert request.domain_daily_caps == {"ready.com": 30}
    with pytest.raises(ValidationError):
        _request(campaign_name="   ")


# --- 5. CLI exit codes and webhook ----------------------------------------------------------


def _run_cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("SMARTLEAD_API_KEY", raising=False)
    monkeypatch.delenv("CAMPAIGN_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "provision_campaign",
            "--campaign-name", "Q1 Outbound",
            "--readiness-report", str(READINESS_FIXTURE),
            "--verification-report", str(VERIFICATION_FIXTURE),
            *args,
        ],
    )
    return main()


def test_cli_mock_mode_prints_report(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_cli(monkeypatch) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["campaign_id"] == 9001
    assert report["attached_domains"] == ["healthy-outreach.com"]
    assert report["uploaded_leads_count"] == 2
    assert report["campaign_status"] == "DRAFTED"


def test_cli_guardrail_block_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readiness = json.loads(READINESS_FIXTURE.read_text(encoding="utf-8"))
    for decision in readiness["decisions"]:
        decision["ready_to_send"] = False
        decision["effective_daily_cap"] = 0
    blocked = tmp_path / "all_blocked.json"
    blocked.write_text(json.dumps(readiness), encoding="utf-8")

    assert _run_cli(monkeypatch, "--readiness-report", str(blocked)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "guardrail no_ready_domains_available" in captured.err


def test_cli_bad_report_file_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _run_cli(monkeypatch, "--verification-report", str(bad)) == 2
    assert "could not load reports" in capsys.readouterr().err


def test_cli_live_mode_requires_api_key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--mode", "live")
    assert exc_info.value.code == 2
    assert "requires --smartlead-api-key" in capsys.readouterr().err


def test_cli_live_auth_error_exits_2_without_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeSmartlead(errors={"GET /email-accounts": _http_error(401)})
    with patch("urllib.request.urlopen", side_effect=fake):
        assert _run_cli(monkeypatch, "--mode", "live", "--smartlead-api-key", FAKE_API_KEY) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rejected the API key (status 401)" in captured.err
    assert FAKE_API_KEY not in captured.err


@patch("urllib.request.urlopen")
def test_cli_posts_report_to_webhook(
    mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_urlopen.return_value.__enter__.return_value.status = 200

    assert _run_cli(monkeypatch, "--webhook-url", WEBHOOK_URL) == 0

    printed = json.loads(capsys.readouterr().out)
    req = mock_urlopen.call_args.args[0]
    assert req.full_url == WEBHOOK_URL
    assert json.loads(req.data.decode("utf-8")) == printed


@patch("urllib.request.urlopen")
def test_cli_prints_report_before_webhook_failure(
    mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")

    assert _run_cli(monkeypatch, "--webhook-url", WEBHOOK_URL) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out)["campaign_id"] == 9001
    assert "Failed to deliver campaign provisioning webhook" in captured.err
