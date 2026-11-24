import json
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gtm_engine.adapters.smartlead_warmup import SmartleadWarmupAdapter

FAKE_API_KEY = "test-key-not-real"


def _account(
    email: str,
    reputation: Any = 95,
    status: str = "ACTIVE",
    smtp_ok: bool = True,
    blocked: bool = False,
    sent: int = 200,
    spam: int = 0,
    max_per_day: int = 40,
) -> dict:
    return {
        "from_email": email,
        "is_smtp_success": smtp_ok,
        "warmup_details": {
            "warmup_reputation": reputation,
            "is_warmup_blocked": blocked,
            "status": status,
            "total_sent_count": sent,
            "total_spam_count": spam,
            "max_email_per_day": max_per_day,
        },
    }


def _mock_accounts_response(mock_urlopen: MagicMock, accounts: list[dict], status: int = 200) -> None:
    resp = mock_urlopen.return_value.__enter__.return_value
    resp.status = status
    resp.read.return_value = json.dumps(accounts).encode("utf-8")


@patch("urllib.request.urlopen")
def test_healthy_domain_passes_with_cap_30(mock_urlopen: MagicMock) -> None:
    _mock_accounts_response(
        mock_urlopen,
        [
            _account("alice@healthy.com", reputation=95),
            _account("bob@healthy.com", reputation="97%"),
            _account("eve@other.com", reputation=10, blocked=True),
        ],
    )

    result = SmartleadWarmupAdapter(FAKE_API_KEY).evaluate_domain("  Healthy.COM ")

    assert result.provider == "smartlead_api"
    assert result.rotation_status == "active"
    assert result.gate_passed is True
    assert result.recommended_daily_cap == 30
    assert result.warmup_score == 95
    assert result.daily_send_limit == 80
    assert result.reasons == []

    req = mock_urlopen.call_args.args[0]
    assert req.get_method() == "GET"
    assert req.full_url.startswith("https://server.smartlead.ai/api/v1/email-accounts?")
    assert f"api_key={FAKE_API_KEY}" in req.full_url


@pytest.mark.parametrize(
    ("account", "expected_reason"),
    [
        (_account("a@risky.com", blocked=True), "warmup_blocked"),
        (_account("a@risky.com", smtp_ok=False), "smtp_disconnected"),
        (_account("a@risky.com", reputation=50), "low_warmup_score:50"),
        (_account("a@risky.com", status="PAUSED"), "warmup_status:PAUSED"),
        (_account("a@risky.com", sent=100, spam=5), "high_spam_rate:5.0"),
    ],
)
@patch("urllib.request.urlopen")
def test_unhealthy_accounts_are_quarantined(
    mock_urlopen: MagicMock, account: dict, expected_reason: str
) -> None:
    _mock_accounts_response(mock_urlopen, [_account("ok@risky.com"), account])

    result = SmartleadWarmupAdapter(FAKE_API_KEY).evaluate_domain("risky.com")

    assert result.rotation_status == "quarantined"
    assert result.gate_passed is False
    assert result.recommended_daily_cap == 0
    assert expected_reason in result.reasons


@patch("urllib.request.urlopen")
def test_mid_reputation_domain_is_throttled(mock_urlopen: MagicMock) -> None:
    _mock_accounts_response(mock_urlopen, [_account("a@warming.com", reputation=78)])

    result = SmartleadWarmupAdapter(FAKE_API_KEY).evaluate_domain("warming.com")

    assert result.rotation_status == "throttled"
    assert result.gate_passed is True
    assert result.recommended_daily_cap == 15
    assert result.reasons == ["warmup_below_target:78"]


@patch("urllib.request.urlopen")
def test_domain_without_accounts_is_quarantined(mock_urlopen: MagicMock) -> None:
    _mock_accounts_response(mock_urlopen, [_account("a@healthy.com")])

    result = SmartleadWarmupAdapter(FAKE_API_KEY).evaluate_domain("unknown.com")

    assert result.rotation_status == "quarantined"
    assert result.gate_passed is False
    assert result.recommended_daily_cap == 0
    assert result.reasons == ["no_smartlead_accounts_found"]


@patch("urllib.request.urlopen")
def test_batch_reuses_cached_accounts_response(mock_urlopen: MagicMock) -> None:
    _mock_accounts_response(
        mock_urlopen,
        [_account("a@one.com"), _account("b@two.com", reputation=50)],
    )
    adapter = SmartleadWarmupAdapter(FAKE_API_KEY)

    results = [adapter.evaluate_domain(d) for d in ["one.com", "two.com", "three.com"]]

    assert mock_urlopen.call_count == 1
    assert [r.rotation_status for r in results] == ["active", "quarantined", "quarantined"]


@patch("urllib.request.urlopen")
def test_api_errors_do_not_leak_api_key(mock_urlopen: MagicMock) -> None:
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url=f"https://server.smartlead.ai/api/v1/email-accounts?api_key={FAKE_API_KEY}",
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )

    with pytest.raises(RuntimeError) as exc_info:
        SmartleadWarmupAdapter(FAKE_API_KEY).evaluate_domain("healthy.com")

    assert "401" in str(exc_info.value)
    assert FAKE_API_KEY not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_blank_api_key_is_rejected() -> None:
    with pytest.raises(ValueError):
        SmartleadWarmupAdapter("   ")
