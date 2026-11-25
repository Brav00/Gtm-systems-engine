import json
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gtm_engine.adapters.lead_sources import (
    FileLeadSourceAdapter,
    GoogleSheetCsvLeadSourceAdapter,
    MockLeadSourceAdapter,
    clean_domain,
    normalize_header,
    to_csv_export_url,
)
from gtm_engine.adapters.smartlead_campaign import _lead_payload
from gtm_engine.adapters.waterfall_verifier import VerificationWaterfall
from gtm_engine.adapters.mock_verifier import MockEmailVerifierAdapter
from gtm_engine.cli.run_pipeline import create_run_dir, main, slugify
from gtm_engine.cli.verify_leads import run_verification
from gtm_engine.contracts.lead_source import LeadInput
from gtm_engine.contracts.leads import LeadInput as LeadsModuleLeadInput
from gtm_engine.ports.lead_source import LeadSourceError

ROOT = Path(__file__).resolve().parent.parent
SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd"
WEBHOOK_URL = "https://hooks.example.test/pipeline"

CLAY_CSV = (
    "﻿First Name,Last Name,Work Email,Company Name,Company Domain,Signal Hook,LinkedIn URL\n"
    "Jane,Doe, Jane.Doe@Acme.COM ,Acme,https://www.Acme.com/about,Hiring for RevOps — indicates investment,https://linkedin.example/jane\n"
    "Sam,Lee,sam@northwind.io,Northwind,northwind.io,,\n"
)
CARGO_CSV = (
    "email,firstname,lastname,company,domain,hook,owner\n"
    "ops@globex.com,Ops,Team,Globex,GLOBEX.com,Raised Series B,alex\n"
)


# --- LeadInput contract ---------------------------------------------------------------------


def test_lead_input_normalizes_and_is_shared() -> None:
    lead = LeadInput(email="  Jane.Doe@ACME.com ", company_domain=" ACME.com ", first_name="  Jane  ", company_name="")
    assert lead.email == "jane.doe@acme.com"
    assert lead.company_domain == "acme.com"
    assert lead.first_name == "Jane"
    assert lead.company_name is None
    assert lead.extra == {}
    assert LeadsModuleLeadInput is LeadInput  # one model for Day 2 and the pipeline


# --- Clay / Cargo CSV and JSON header mapping -----------------------------------------------


@pytest.mark.parametrize(
    ("header", "normalized"),
    [("Work Email", "work_email"), ("work-email", "work_email"), (" EMAIL ", "email"), ("Company Domain", "company_domain")],
)
def test_normalize_header(header: str, normalized: str) -> None:
    assert normalize_header(header) == normalized


def test_clean_domain() -> None:
    assert clean_domain("https://www.Acme.com/about?x=1") == "acme.com"
    assert clean_domain("GLOBEX.com") == "globex.com"


def test_clay_csv_headers_map_to_lead_fields(tmp_path: Path) -> None:
    path = tmp_path / "clay_export.csv"
    path.write_text(CLAY_CSV, encoding="utf-8")

    jane, sam = FileLeadSourceAdapter().fetch_leads(str(path))

    assert jane.email == "jane.doe@acme.com"
    assert (jane.first_name, jane.last_name, jane.company_name) == ("Jane", "Doe", "Acme")
    assert jane.company_domain == "acme.com"
    assert jane.signal_hook == "Hiring for RevOps — indicates investment"
    assert jane.extra == {"LinkedIn URL": "https://linkedin.example/jane"}
    assert sam.signal_hook is None
    assert sam.extra == {}


def test_cargo_csv_headers_map_to_lead_fields(tmp_path: Path) -> None:
    path = tmp_path / "cargo.csv"
    path.write_text(CARGO_CSV, encoding="utf-8")

    (lead,) = FileLeadSourceAdapter().fetch_leads(str(path))

    assert lead.email == "ops@globex.com"
    assert (lead.first_name, lead.last_name) == ("Ops", "Team")
    assert lead.company_name == "Globex"
    assert lead.company_domain == "globex.com"
    assert lead.signal_hook == "Raised Series B"
    assert lead.extra == {"owner": "alex"}


def test_email_alias_priority_and_blank_email_rows(tmp_path: Path) -> None:
    path = tmp_path / "mixed.csv"
    path.write_text("Email,Work Email\n,work@acme.com\nprimary@acme.com,work@acme.com\n,\n", encoding="utf-8")

    leads = FileLeadSourceAdapter().fetch_leads(str(path))

    assert [lead.email for lead in leads] == ["work@acme.com", "primary@acme.com", ""]


@pytest.mark.parametrize(
    "payload",
    [
        [{"Work Email": "A@acme.com", "First Name": "A"}],
        {"leads": [{"email": "a@acme.com", "first_name": "A"}]},
    ],
    ids=["list", "leads_wrapper"],
)
def test_json_lead_files(tmp_path: Path, payload: Any) -> None:
    path = tmp_path / "leads.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    (lead,) = FileLeadSourceAdapter().fetch_leads(str(path))
    assert (lead.email, lead.first_name) == ("a@acme.com", "A")


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("missing.csv", None, "lead file not found"),
        ("leads.txt", "email\na@b.com", "unsupported file type"),
        ("no_email.csv", "name,company\nJane,Acme\n", "has no email column"),
        ("empty.csv", "", "has no header row"),
        ("broken.json", "{not json", "is not valid JSON"),
        ("wrong.json", json.dumps({"rows": []}), "must contain a JSON list"),
        ("strings.json", json.dumps(["a@b.com"]), "lead 1 is not a JSON object"),
    ],
)
def test_file_source_errors(tmp_path: Path, filename: str, content: str | None, message: str) -> None:
    path = tmp_path / filename
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(LeadSourceError, match=message):
        FileLeadSourceAdapter().fetch_leads(str(path))


# --- Google Sheets URL normalization and CSV parsing ----------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (SHEET_ID, f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"),
        (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=12345",
         f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=12345"),
        (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?usp=sharing&gid=7",
         f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=7"),
        (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0",
         f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0"),
        ("https://docs.google.com/spreadsheets/d/e/2PACX-pub123/pubhtml?gid=5",
         "https://docs.google.com/spreadsheets/d/e/2PACX-pub123/pub?output=csv&gid=5"),
        ("https://exports.example.com/leads.csv?token=abc", "https://exports.example.com/leads.csv?token=abc"),
    ],
    ids=["bare_id", "edit_fragment_gid", "edit_query_gid", "export_url", "published", "direct_csv"],
)
def test_sheet_url_normalization(ref: str, expected: str) -> None:
    assert to_csv_export_url(ref) == expected


@pytest.mark.parametrize(
    "ref",
    ["http://docs.google.com/spreadsheets/d/x", "file:///etc/passwd", "not a url", "https://docs.google.com/spreadsheets/d/short/edit"],
)
def test_sheet_url_rejects_unsafe_or_invalid(ref: str) -> None:
    with pytest.raises(LeadSourceError):
        to_csv_export_url(ref)


def _http_response(body: bytes, status: int = 200) -> MagicMock:
    cm = MagicMock()
    cm.__enter__.return_value.status = status
    cm.__enter__.return_value.read.return_value = body
    return cm


def test_gsheet_fetches_export_url_and_parses_csv() -> None:
    with patch("urllib.request.urlopen", return_value=_http_response(CLAY_CSV.encode("utf-8"))) as mock_urlopen:
        leads = GoogleSheetCsvLeadSourceAdapter().fetch_leads(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=9")

    req = mock_urlopen.call_args.args[0]
    assert req.full_url == f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=9"
    assert [lead.email for lead in leads] == ["jane.doe@acme.com", "sam@northwind.io"]


@pytest.mark.parametrize("code", [401, 403, 404, 500])
def test_gsheet_http_errors_mask_url_and_tokens(code: int) -> None:
    secret_url = "https://exports.example.com/leads.csv?token=super-secret-token"
    error = urllib.error.HTTPError(secret_url, code, "error", None, None)  # type: ignore[arg-type]
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(LeadSourceError) as exc_info:
            GoogleSheetCsvLeadSourceAdapter().fetch_leads(secret_url)

    message = str(exc_info.value)
    assert f"status {code}" in message
    assert "super-secret-token" not in message
    assert "exports.example.com" not in message
    assert exc_info.value.__cause__ is None
    assert ("Anyone with the link" in message) is (code in (401, 403, 404))


def test_gsheet_login_page_is_reported_not_parsed() -> None:
    page = b"<!DOCTYPE html><html><body>Sign in</body></html>"
    with patch("urllib.request.urlopen", return_value=_http_response(page)):
        with pytest.raises(LeadSourceError, match="returned a web page instead of CSV"):
            GoogleSheetCsvLeadSourceAdapter().fetch_leads(SHEET_ID)


# --- Lead context reaches Smartlead ---------------------------------------------------------


def test_company_name_and_hook_reach_smartlead_payload() -> None:
    lead = LeadInput(email="jane@acme.com", first_name="Jane", company_name="Acme", company_domain="acme.com",
                     signal_hook="Hiring for RevOps")
    (result,) = run_verification([lead], VerificationWaterfall([MockEmailVerifierAdapter()])).results

    assert _lead_payload(result) == {
        "email": "jane@acme.com",
        "first_name": "Jane",
        "company_name": "Acme",
        "website": "acme.com",
        "custom_fields": {"signal_hook": "Hiring for RevOps"},
    }


# --- Full pipeline runs via the CLI ---------------------------------------------------------


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str) -> int:
    monkeypatch.chdir(ROOT)
    for name in ("VERIFIER_API_KEY", "MILLIONVERIFIER_API_KEY", "SMARTLEAD_API_KEY", "PIPELINE_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    defaults = ["--campaign-name", "Q1 RevOps Push", "--output-dir", str(tmp_path / "runs")]
    return main([*defaults, *args])


def _summary(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_full_mock_run_stays_drafted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _run(monkeypatch, tmp_path, "--source-type", "mock", "--domains", "healthy-outreach.com,quarantined-sender.com")
    summary = _summary(capsys)

    assert code == 0
    assert summary["status"] == "provisioned"
    assert summary["campaign_id"] == 9001
    assert summary["campaign_status"] == "DRAFTED"
    assert summary["launch_requested"] is False
    assert summary["leads_loaded"] == 7
    assert (summary["valid_leads"], summary["risky_leads"], summary["invalid_leads"], summary["unknown_leads"]) == (3, 1, 2, 1)
    assert summary["ready_domains"] == ["healthy-outreach.com"]
    assert summary["blocked_domains"] == ["quarantined-sender.com"]
    assert summary["uploaded_leads"] == 2  # the duplicate Jane is uploaded once
    assert summary["total_daily_capacity"] == 30

    run_dir = Path(summary["output_dir"])
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "01_raw_leads.json",
        "02_verification_report.json",
        "03_readiness_report.json",
        "04_provisioning_report.json",
        "run_summary.json",
    ]
    assert json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8")) == summary
    provisioning = json.loads((run_dir / "04_provisioning_report.json").read_text(encoding="utf-8"))
    assert provisioning["campaign_status"] == "DRAFTED"


def test_full_mock_run_with_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _run(monkeypatch, tmp_path, "--source-type", "mock", "--domains", "healthy-outreach.com", "--launch")
    summary = _summary(capsys)
    assert code == 0
    assert summary["launch_requested"] is True
    assert summary["campaign_status"] == "ACTIVE"
    assert "campaign_activated" in summary["reasons"]


def test_file_source_run_with_clay_csv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    csv_path = tmp_path / "clay.csv"
    csv_path.write_text(CLAY_CSV, encoding="utf-8")
    code = _run(monkeypatch, tmp_path, "--source-type", "file", "--source-ref", str(csv_path), "--domains", "healthy-outreach.com")
    summary = _summary(capsys)
    assert code == 0
    assert summary["source_type"] == "file"
    assert summary["leads_loaded"] == 2
    assert summary["uploaded_leads"] == 2


def test_runs_never_overwrite_each_other(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
    first = create_run_dir(tmp_path, now)
    second = create_run_dir(tmp_path, now)
    assert first[0] == "20261125T120000Z"
    assert second[0] == "20261125T120000Z-2"
    assert first[1] != second[1] and first[1].is_dir() and second[1].is_dir()
    assert slugify("  Q1: RevOps Push!! ") == "q1-revops-push"
    assert slugify("!!!") == "campaign"


def test_default_output_dir_is_runs_campaign_slug(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    for fixture in ("sample_domains.json", "sample_warmup.json"):
        (tmp_path / "fixtures").mkdir(exist_ok=True)
        (tmp_path / "fixtures" / fixture).write_text((ROOT / "fixtures" / fixture).read_text(encoding="utf-8"), encoding="utf-8")
    code = main(["--source-type", "mock", "--domains", "healthy-outreach.com", "--campaign-name", "Q1 RevOps Push"])
    summary = _summary(capsys)
    assert code == 0
    assert Path(summary["output_dir"]).parent == Path("runs") / "q1-revops-push"


# --- Guardrail blocks (exit 1) --------------------------------------------------------------


def test_no_ready_domains_blocks_with_exit_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _run(monkeypatch, tmp_path, "--source-type", "mock", "--domains", "quarantined-sender.com,broken-sender.com")
    captured = capsys.readouterr()
    summary = json.loads(captured.out)

    assert code == 1
    assert summary["status"] == "blocked"
    assert "guardrail:no_ready_domains_available" in summary["reasons"]
    assert "domain_blocked:quarantined-sender.com:warmup:low_warmup_score:52" in summary["reasons"]
    assert summary["reasons"][-1] == "no_campaign_changes_made"
    assert "provisioning" not in summary["artifacts"]
    assert Path(summary["artifacts"]["run_summary"]).is_file()  # summary still persisted
    assert "blocked: guardrail:no_ready_domains_available" in captured.err


def test_no_valid_leads_blocks_with_exit_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    leads = tmp_path / "bad_leads.json"
    leads.write_text(json.dumps([{"email": "bounce@acme.com"}, {"email": "ops@catchall.com"}, {"email": "nope"}]), encoding="utf-8")
    code = _run(monkeypatch, tmp_path, "--source-type", "file", "--source-ref", str(leads), "--domains", "healthy-outreach.com")
    summary = _summary(capsys)

    assert code == 1
    assert summary["reasons"][0] == "guardrail:no_valid_leads_to_upload"
    assert "lead_counts:loaded=3,risky=1,invalid=2,unknown=0" in summary["reasons"]
    assert summary["ready_domains"] == ["healthy-outreach.com"]


def test_both_guardrails_reported_together(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    leads = tmp_path / "bad.json"
    leads.write_text(json.dumps([{"email": "bounce@acme.com"}]), encoding="utf-8")
    assert _run(monkeypatch, tmp_path, "--source-type", "file", "--source-ref", str(leads), "--domains", "broken-sender.com") == 1
    reasons = _summary(capsys)["reasons"]
    assert reasons[:2] == ["guardrail:no_ready_domains_available", "guardrail:no_valid_leads_to_upload"]


# --- Errors (exit 2) ------------------------------------------------------------------------


def test_missing_source_file_exits_2_and_persists_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run(monkeypatch, tmp_path, "--source-type", "file", "--source-ref", "nope/leads.csv", "--domains", "healthy-outreach.com")
    captured = capsys.readouterr()
    summary = json.loads(captured.out)

    assert code == 2
    assert summary["status"] == "error"
    assert summary["reasons"] == ["load_leads_failed:lead file not found: nope/leads.csv"]
    assert Path(summary["artifacts"]["run_summary"]).is_file()
    assert "load_leads_failed" in captured.err


def test_gsheet_network_failure_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Name or service not known")):
        code = _run(monkeypatch, tmp_path, "--source-type", "gsheet", "--source-ref", SHEET_ID, "--domains", "healthy-outreach.com")
    summary = _summary(capsys)
    assert code == 2
    assert summary["reasons"] == ["load_leads_failed:Failed to reach Google Sheets: Name or service not known"]
    assert SHEET_ID not in json.dumps(summary)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--source-type", "file", "--domains", "a.com"], "requires --source-ref"),
        (["--source-type", "mock", "--domains", " , "], "needs at least one sending domain"),
        (["--source-type", "mock", "--domains", "a.com", "--campaign-id", "0"], "positive integer"),
        (["--source-type", "csv", "--domains", "a.com"], "invalid choice"),
        (["--source-type", "mock", "--domains", "a.com", "--mode", "live"], "--mode live requires --verifier-api-key"),
    ],
    ids=["file_without_ref", "no_domains", "bad_campaign_id", "bad_source_type", "live_without_keys"],
)
def test_invalid_cli_args_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], args: list[str], message: str
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run(monkeypatch, tmp_path, *args)
    assert exc_info.value.code == 2
    assert message in capsys.readouterr().err


def test_live_missing_one_key_names_flag_not_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _run(monkeypatch, tmp_path, "--source-type", "mock", "--domains", "a.com", "--mode", "live",
             "--verifier-api-key", "mv-secret-value")
    err = capsys.readouterr().err
    assert "--smartlead-api-key (or SMARTLEAD_API_KEY)" in err
    assert "mv-secret-value" not in err


# --- Webhook --------------------------------------------------------------------------------


def test_webhook_receives_run_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.status = 200
        code = _run(monkeypatch, tmp_path, "--source-type", "mock", "--domains", "healthy-outreach.com", "--webhook-url", WEBHOOK_URL)
    summary = _summary(capsys)
    assert code == 0
    req = mock_urlopen.call_args.args[0]
    assert req.full_url == WEBHOOK_URL
    assert json.loads(req.data.decode("utf-8")) == summary


def test_webhook_failure_after_provisioning_exits_2_with_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        code = _run(monkeypatch, tmp_path, "--source-type", "mock", "--domains", "healthy-outreach.com", "--webhook-url", WEBHOOK_URL)
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["status"] == "provisioned"
    assert "do not re-run without --campaign-id" in captured.err


def test_mock_source_is_deterministic() -> None:
    first = MockLeadSourceAdapter().fetch_leads("")
    assert first == MockLeadSourceAdapter().fetch_leads("ignored")
    assert len(first) == 7
    assert first[1].email == "sam.lee@northwind-logistics.com"
