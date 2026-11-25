import json
import sys
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from gtm_engine.adapters.greenhouse_signals import LiveGreenhouseSignalAdapter
from gtm_engine.adapters.mock_signals import MockSignalCollectorAdapter
from gtm_engine.cli.collect_signals import load_targets, main
from gtm_engine.contracts.signals import (
    JobPosting,
    SignalTargetBoard,
    build_signal_hook,
    build_signal_report,
    make_signal_id,
    match_keywords,
)

ROOT = Path(__file__).resolve().parent.parent
TARGETS_FIXTURE = ROOT / "fixtures" / "sample_signal_targets.json"
WEBHOOK_URL = "https://hooks.example.test/signals"
FIXED_NOW = "2026-02-01T12:00:00+00:00"

ACME = SignalTargetBoard(company_name="Acme", company_domain="Acme.COM", board_token="acme")
GLOBEX = SignalTargetBoard(company_name="Globex", company_domain="globex.com", board_token="globex")
GONE = SignalTargetBoard(company_name="Gone Inc", company_domain="gone.io", board_token="gone-inc")


def _job(job_id: int, title: str, location: str | None = "Remote", token: str = "acme") -> dict[str, Any]:
    """Shaped like a real boards-api.greenhouse.io job entry."""
    return {
        "absolute_url": f"https://job-boards.greenhouse.io/{token}/jobs/{job_id}",
        "data_compliance": [{"type": "gdpr", "requires_consent": False}],
        "internal_job_id": job_id + 1000,
        "location": {"name": location} if location is not None else {},
        "metadata": None,
        "id": job_id,
        "updated_at": "2026-01-20T10:15:00-05:00",
        "requisition_id": f"REQ-{job_id}",
        "title": title,
        "company_name": token.title(),
        "first_published": "2026-01-10T09:00:00-05:00",
    }


def _board(*jobs: dict[str, Any]) -> dict[str, Any]:
    return {"jobs": list(jobs), "meta": {"total": len(jobs)}}


class FakeGreenhouse:
    """Stands in for urllib.request.urlopen, serving boards by token or raising per board."""

    def __init__(self, boards: dict[str, dict[str, Any]], errors: dict[str, Exception] | None = None) -> None:
        self.boards = boards
        self.errors = errors or {}
        self.urls: list[str] = []

    def __call__(self, req: Any, timeout: float | None = None) -> MagicMock:
        self.urls.append(req.full_url)
        token = req.full_url.rstrip("/").split("/")[-2]
        if token in self.errors:
            raise self.errors[token]
        cm = MagicMock()
        resp = cm.__enter__.return_value
        resp.status = 200
        resp.read.return_value = json.dumps(self.boards[token]).encode("utf-8")
        return cm


def _not_found(token: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        code=404,
        msg="Not Found",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


def _live() -> LiveGreenhouseSignalAdapter:
    return LiveGreenhouseSignalAdapter(clock=lambda: FIXED_NOW)


# --- 1. Keyword filtering, matched_keywords and hooks ---------------------------------------


def test_keyword_match_is_case_insensitive_substring() -> None:
    assert match_keywords("Senior REVOPS Manager", ["revops", "Sales"]) == ["revops"]
    assert match_keywords("VP, Sales & Growth", ["growth", "SALES"]) == ["growth", "SALES"]
    assert match_keywords("Backend Engineer", ["Growth"]) == []
    assert match_keywords("Anything", []) == []


def test_matches_populate_keywords_and_hook() -> None:
    postings = [
        JobPosting(ACME, "Senior RevOps Manager", "NYC", "https://example.test/1"),
        JobPosting(ACME, "Backend Engineer", "NYC", "https://example.test/2"),
        JobPosting(ACME, "growth marketing lead", None, "https://example.test/3"),
    ]

    report = build_signal_report("test", postings, [" RevOps ", "GROWTH", "revops", ""], detected_at=FIXED_NOW)

    assert report.total_scanned == 3
    assert report.matched_count == 2
    first, second = report.signals
    assert first.title == "Senior RevOps Manager"
    assert first.matched_keywords == ["RevOps"]
    assert first.signal_hook == "Hiring for Senior RevOps Manager — indicates investment in revenue operations"
    assert first.signal_type == "hiring_intent"
    assert first.company_domain == "acme.com"
    assert first.detected_at == FIXED_NOW
    assert second.matched_keywords == ["GROWTH"]
    assert second.signal_hook.endswith("indicates active growth and marketing investment")


def test_empty_keywords_return_every_role() -> None:
    postings = [JobPosting(ACME, t, None, "https://example.test") for t in ["Backend Engineer", "Designer"]]
    report = build_signal_report("test", postings, [], detected_at=FIXED_NOW)
    assert report.matched_count == 2
    assert all(s.matched_keywords == [] for s in report.signals)


@pytest.mark.parametrize(
    ("title", "theme"),
    [
        ("Senior RevOps Manager", "investment in revenue operations"),
        ("Chief Revenue Officer", "new GTM leadership"),
        ("Enterprise Account Executive", "active sales team expansion"),
        ("Demand Generation Manager", "active growth and marketing investment"),
        ("Revenue Accountant", "active GTM scaling"),
        ("Salesforce Developer", "active GTM scaling"),
    ],
)
def test_hook_themes(title: str, theme: str) -> None:
    assert build_signal_hook(title, []) == f"Hiring for {title} — indicates {theme}"


# --- 2. Deterministic signal IDs and deduplication ------------------------------------------


def test_signal_id_is_deterministic_and_normalized() -> None:
    a = make_signal_id("acme.com", "hiring_intent", "Senior RevOps Manager")
    assert a == make_signal_id("ACME.com", "hiring_intent", "  senior   revops manager ")
    assert a.startswith("sig_") and len(a) == 20
    assert a != make_signal_id("globex.com", "hiring_intent", "Senior RevOps Manager")
    assert a != make_signal_id("acme.com", "funding_or_expansion", "Senior RevOps Manager")


def test_duplicate_postings_deduplicate_by_signal_id() -> None:
    postings = [
        JobPosting(ACME, "Account Executive", "Chicago", "https://example.test/1"),
        JobPosting(ACME, "account  executive", "Austin", "https://example.test/2"),
        JobPosting(GLOBEX, "Account Executive", "Chicago", "https://example.test/3"),
    ]

    report = build_signal_report("test", postings, ["account executive"], detected_at=FIXED_NOW)

    assert report.total_scanned == 3
    assert report.matched_count == 2  # same title at the same company counts once
    assert [s.company_name for s in report.signals] == ["Acme", "Globex"]
    assert report.signals[0].location == "Chicago"  # first posting wins
    assert len({s.signal_id for s in report.signals}) == 2


def test_mock_collector_is_deterministic_and_dedupes() -> None:
    targets, keywords = load_targets(TARGETS_FIXTURE)
    first = MockSignalCollectorAdapter().collect_signals(targets, ["RevOps", "Account Executive", "Sales"])
    second = MockSignalCollectorAdapter().collect_signals(targets, ["RevOps", "Account Executive", "Sales"])

    assert first == second
    assert first.provider == "mock"
    assert [s.title for s in first.signals] == [
        "Senior RevOps Manager",
        "Enterprise Account Executive",
        "Sales Development Representative",
    ]
    assert first.total_scanned == 12
    assert first.unique_companies == ["Stripe", "Figma", "Vercel"]
    assert keywords == ["Growth", "Revenue", "Sales", "Marketing"]


# --- 3. Live Greenhouse adapter --------------------------------------------------------------


def test_live_adapter_parses_greenhouse_payload() -> None:
    fake = FakeGreenhouse(
        {
            "acme": _board(
                _job(1, "Senior RevOps Manager", "New York, NY"),
                _job(2, "Staff Backend Engineer"),
                _job(3, "Growth Marketing Lead", None),
                _job(4, "Senior RevOps Manager", "London, UK"),
            ),
            "globex": _board(_job(9, "Enterprise Account Executive", "Chicago, IL", token="globex")),
        }
    )

    with patch("urllib.request.urlopen", side_effect=fake):
        report = _live().collect_signals([ACME, GLOBEX], ["revops", "growth", "Account Executive"])

    assert fake.urls == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        "https://boards-api.greenhouse.io/v1/boards/globex/jobs",
    ]
    assert report.provider == "greenhouse"
    assert report.total_scanned == 5
    assert report.matched_count == 3
    assert report.errors == []
    revops, growth, ae = report.signals
    assert revops.location == "New York, NY"
    assert revops.evidence_url == "https://job-boards.greenhouse.io/acme/jobs/1"
    assert revops.detected_at == FIXED_NOW
    assert growth.location is None
    assert ae.company_domain == "globex.com"
    assert ae.signal_hook == "Hiring for Enterprise Account Executive — indicates active sales team expansion"


def test_live_adapter_records_404_and_keeps_healthy_boards() -> None:
    fake = FakeGreenhouse(
        {
            "acme": _board(_job(1, "Head of Growth")),
            "globex": _board(_job(2, "VP Sales", token="globex")),
        },
        errors={"gone-inc": _not_found("gone-inc")},
    )

    with patch("urllib.request.urlopen", side_effect=fake):
        report = _live().collect_signals([ACME, GONE, GLOBEX], ["growth", "sales"])

    assert len(fake.urls) == 3  # kept going after the 404
    assert report.errors == ["greenhouse:gone-inc:http_404"]
    assert report.matched_count == 2
    assert report.unique_companies == ["Acme", "Globex"]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (urllib.error.URLError("Name or service not known"), "greenhouse:gone-inc:unreachable:Name or service not known"),
        (TimeoutError("timed out"), "greenhouse:gone-inc:timeout"),
    ],
)
def test_live_adapter_records_network_failures(failure: Exception, expected: str) -> None:
    fake = FakeGreenhouse({"acme": _board(_job(1, "Head of Growth"))}, errors={"gone-inc": failure})
    with patch("urllib.request.urlopen", side_effect=fake):
        report = _live().collect_signals([GONE, ACME], ["growth"])
    assert report.errors == [expected]
    assert report.matched_count == 1


def test_live_adapter_records_invalid_json() -> None:
    cm = MagicMock()
    cm.__enter__.return_value.status = 200
    cm.__enter__.return_value.read.return_value = b"<html>not json</html>"
    with patch("urllib.request.urlopen", return_value=cm):
        report = _live().collect_signals([ACME], ["growth"])
    assert report.errors == ["greenhouse:acme:invalid_json"]


def test_target_validation() -> None:
    assert SignalTargetBoard(company_name=" Acme ", company_domain=" ACME.com ", board_token="acme_1").company_domain == "acme.com"
    for bad in ({"board_token": "../admin"}, {"board_token": "a b"}, {"company_domain": "not-a-domain"}, {"company_name": " "}):
        fields = {"company_name": "Acme", "company_domain": "acme.com", "board_token": "acme", **bad}
        with pytest.raises(ValidationError):
            SignalTargetBoard(**fields)


# --- 4. CLI exit codes -----------------------------------------------------------------------


def _run_cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("SIGNAL_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["collect_signals", *args])
    return main()


def test_cli_finds_signals_exit_0(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_cli(monkeypatch, "--strict") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["matched_count"] == 3
    assert {s["title"] for s in report["signals"]} == {
        "Growth Marketing Lead",
        "Head of Revenue Operations",
        "Sales Development Representative",
    }


def test_cli_keywords_flag_overrides_file(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_cli(monkeypatch, "--keywords", "RevOps, ") == 0
    report = json.loads(capsys.readouterr().out)
    assert [s["title"] for s in report["signals"]] == ["Senior RevOps Manager"]


def test_cli_strict_with_no_matches_exit_1(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_cli(monkeypatch, "--keywords", "Quantum Underwater Basket Weaver") == 0
    capsys.readouterr()
    assert _run_cli(monkeypatch, "--keywords", "Quantum Underwater Basket Weaver", "--strict") == 1
    assert json.loads(capsys.readouterr().out)["matched_count"] == 0


@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps({"targets": []}), json.dumps([{"company_name": "X", "company_domain": "x.com", "board_token": "../x"}])],
    ids=["invalid_json", "no_targets", "bad_token"],
)
def test_cli_invalid_input_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    targets = tmp_path / "targets.json"
    targets.write_text(content, encoding="utf-8")
    assert _run_cli(monkeypatch, "--targets-file", str(targets)) == 2
    assert "could not load targets" in capsys.readouterr().err


def test_cli_missing_file_exit_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_cli(monkeypatch, "--targets-file", "does/not/exist.json") == 2
    assert "could not load targets" in capsys.readouterr().err


def test_cli_live_all_boards_failed_exit_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGreenhouse({}, errors={t: _not_found(t) for t in ["stripe", "figma", "vercel"]})
    with patch("urllib.request.urlopen", side_effect=fake):
        assert _run_cli(monkeypatch, "--mode", "live") == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["errors"] == [
        "greenhouse:stripe:http_404",
        "greenhouse:figma:http_404",
        "greenhouse:vercel:http_404",
    ]
    assert "every target failed" in captured.err


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
    assert json.loads(captured.out)["matched_count"] == 3
    assert "Failed to deliver intent signals webhook" in captured.err
