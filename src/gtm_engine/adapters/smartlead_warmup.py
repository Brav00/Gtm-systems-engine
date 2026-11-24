import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from gtm_engine.contracts.warmup import DomainWarmupEvaluation, RotationStatus


def _parse_reputation(value: Any) -> int:
    """Smartlead may return reputation as an int, a float, or a string like "95%"."""
    if value is None:
        return 0
    try:
        score = int(float(str(value).strip().rstrip("%")))
    except ValueError:
        return 0
    return max(0, min(100, score))


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class SmartleadWarmupAdapter:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://server.smartlead.ai/api/v1",
        timeout_seconds: float = 15.0,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._accounts_cache: list[dict] | None = None

    def _fetch_accounts(self) -> list[dict]:
        if self._accounts_cache is not None:
            return self._accounts_cache

        url = f"{self.base_url}/email-accounts?{urllib.parse.urlencode({'api_key': self.api_key})}"
        req = urllib.request.Request(url=url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise RuntimeError(f"Smartlead API returned unexpected status: {resp.status}")
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Never include the request URL in errors: it carries the API key.
            raise RuntimeError(f"Smartlead API request failed with status {exc.code}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Failed to reach Smartlead API: {exc.reason}") from None

        if isinstance(data, dict):
            data = data.get("data", data.get("email_accounts", []))
        self._accounts_cache = [acc for acc in data if isinstance(acc, dict)] if isinstance(data, list) else []
        return self._accounts_cache

    def evaluate_domain(self, domain: str) -> DomainWarmupEvaluation:
        target_domain = domain.strip().lower()
        accounts = [
            acc
            for acc in self._fetch_accounts()
            if str(acc.get("from_email", "")).strip().lower().endswith(f"@{target_domain}")
        ]

        if not accounts:
            return DomainWarmupEvaluation(
                domain=target_domain,
                provider="smartlead_api",
                warmup_score=0,
                bounce_rate_pct=0.0,
                daily_send_limit=0,
                recommended_daily_cap=0,
                rotation_status="quarantined",
                gate_passed=False,
                reasons=["no_smartlead_accounts_found"],
            )

        reasons: list[str] = []
        reputations: list[int] = []
        spam_rates: list[float] = []
        daily_send_limit = 0

        for acc in accounts:
            details = acc.get("warmup_details") or {}
            reputations.append(_parse_reputation(details.get("warmup_reputation")))

            sent = _as_int(details.get("total_sent_count"))
            spam = _as_int(details.get("total_spam_count"))
            spam_rates.append(min(100.0, (spam / sent) * 100) if sent > 0 else 0.0)

            daily_send_limit += _as_int(details.get("max_email_per_day"))

            if acc.get("is_smtp_success") is False and "smtp_disconnected" not in reasons:
                reasons.append("smtp_disconnected")
            if details.get("is_warmup_blocked") is True and "warmup_blocked" not in reasons:
                reasons.append("warmup_blocked")
            status_value = details.get("status")
            if status_value != "ACTIVE" and f"warmup_status:{status_value}" not in reasons:
                reasons.append(f"warmup_status:{status_value}")

        min_reputation = min(reputations)
        spam_rate_pct = max(spam_rates)

        if min_reputation < 70:
            reasons.append(f"low_warmup_score:{min_reputation}")
        if spam_rate_pct > 3.0:
            reasons.append(f"high_spam_rate:{spam_rate_pct:.1f}")

        # Same active / throttled / quarantined gate as MockWarmupMonitorAdapter.
        if reasons:
            status: RotationStatus = "quarantined"
            recommended_cap = 0
            gate_passed = False
        elif min_reputation < 85 or spam_rate_pct > 1.5:
            status = "throttled"
            recommended_cap = min(daily_send_limit, 15)
            gate_passed = True
            if min_reputation < 85:
                reasons.append(f"warmup_below_target:{min_reputation}")
            if spam_rate_pct > 1.5:
                reasons.append(f"elevated_spam_rate:{spam_rate_pct:.1f}")
        else:
            status = "active"
            recommended_cap = min(daily_send_limit, 30)
            gate_passed = True

        return DomainWarmupEvaluation(
            domain=target_domain,
            provider="smartlead_api",
            warmup_score=min_reputation,
            bounce_rate_pct=round(spam_rate_pct, 2),
            daily_send_limit=daily_send_limit,
            recommended_daily_cap=recommended_cap,
            rotation_status=status,
            gate_passed=gate_passed,
            reasons=reasons,
        )
