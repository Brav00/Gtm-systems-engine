import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gtm_engine.adapters.http_webhook_notifier import HttpWebhookReadinessNotifier
from gtm_engine.adapters.mock_dns import MockDeliverabilityAdapter
from gtm_engine.adapters.mock_warmup import MockWarmupMonitorAdapter
from gtm_engine.cli.check_readiness import dispatch_readiness_notification, main, run_readiness_check

ROOT = Path(__file__).resolve().parent.parent
WEBHOOK_URL = "https://hooks.example.test/readiness"


def _adapters() -> tuple[MockDeliverabilityAdapter, MockWarmupMonitorAdapter]:
    return (
        MockDeliverabilityAdapter(ROOT / "fixtures" / "sample_domains.json"),
        MockWarmupMonitorAdapter(ROOT / "fixtures" / "sample_warmup.json"),
    )


def _mock_ok_response(mock_urlopen: MagicMock, status: int = 200) -> None:
    mock_urlopen.return_value.__enter__.return_value.status = status


@patch("urllib.request.urlopen")
def test_http_webhook_notifier_posts_json_payload(mock_urlopen: MagicMock) -> None:
    _mock_ok_response(mock_urlopen)
    dns, warmup = _adapters()
    report = run_readiness_check(
        domains=["healthy-outreach.com", "quarantined-sender.com"],
        dns_auditor=dns,
        warmup_monitor=warmup,
    )

    HttpWebhookReadinessNotifier(WEBHOOK_URL, timeout_seconds=5.0).notify_readiness(report)

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args.args[0]
    assert req.full_url == WEBHOOK_URL
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    assert mock_urlopen.call_args.kwargs["timeout"] == 5.0

    payload = json.loads(req.data.decode("utf-8"))
    assert payload == report.model_dump(mode="json")
    assert payload["total_domains"] == 2
    assert payload["blocked_count"] == 1


@patch("urllib.request.urlopen")
def test_http_webhook_notifier_raises_on_non_2xx_status(mock_urlopen: MagicMock) -> None:
    _mock_ok_response(mock_urlopen, status=302)
    dns, warmup = _adapters()
    report = run_readiness_check(domains=["healthy-outreach.com"], dns_auditor=dns, warmup_monitor=warmup)

    with pytest.raises(RuntimeError, match="unexpected status: 302"):
        HttpWebhookReadinessNotifier(WEBHOOK_URL).notify_readiness(report)


def test_http_webhook_notifier_rejects_empty_url() -> None:
    with pytest.raises(ValueError):
        HttpWebhookReadinessNotifier("   ")


@patch("urllib.request.urlopen")
def test_dispatch_notifies_when_only_blocked_false(mock_urlopen: MagicMock) -> None:
    _mock_ok_response(mock_urlopen)
    dns, warmup = _adapters()
    report = run_readiness_check(domains=["healthy-outreach.com"], dns_auditor=dns, warmup_monitor=warmup)

    sent = dispatch_readiness_notification(report, HttpWebhookReadinessNotifier(WEBHOOK_URL), only_blocked=False)

    assert report.blocked_count == 0
    assert sent is True
    mock_urlopen.assert_called_once()


@patch("urllib.request.urlopen")
def test_dispatch_skips_notify_when_only_blocked_and_nothing_blocked(mock_urlopen: MagicMock) -> None:
    _mock_ok_response(mock_urlopen)
    dns, warmup = _adapters()
    report = run_readiness_check(domains=["healthy-outreach.com"], dns_auditor=dns, warmup_monitor=warmup)
    notifier = HttpWebhookReadinessNotifier(WEBHOOK_URL)

    with patch.object(notifier, "notify_readiness", wraps=notifier.notify_readiness) as spy:
        sent = dispatch_readiness_notification(report, notifier, only_blocked=True)

    assert report.blocked_count == 0
    assert sent is False
    spy.assert_not_called()
    mock_urlopen.assert_not_called()


@patch("urllib.request.urlopen")
def test_dispatch_notifies_when_only_blocked_and_something_blocked(mock_urlopen: MagicMock) -> None:
    _mock_ok_response(mock_urlopen)
    dns, warmup = _adapters()
    report = run_readiness_check(
        domains=["healthy-outreach.com", "quarantined-sender.com"],
        dns_auditor=dns,
        warmup_monitor=warmup,
    )

    sent = dispatch_readiness_notification(report, HttpWebhookReadinessNotifier(WEBHOOK_URL), only_blocked=True)

    assert report.blocked_count == 1
    assert sent is True
    mock_urlopen.assert_called_once()


@patch("urllib.request.urlopen")
def test_main_prints_report_before_webhook_failure(
    mock_urlopen: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_urlopen.side_effect = urllib.error.URLError("connection refused")
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_readiness", "--domains", "healthy-outreach.com", "--webhook-url", WEBHOOK_URL],
    )

    exit_code = main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.out)["total_domains"] == 1
    assert "Failed to deliver readiness webhook" in captured.err
    mock_urlopen.assert_called_once()
