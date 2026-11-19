import json
from pathlib import Path
from gtm_engine.contracts.warmup import DomainWarmupEvaluation, RotationStatus


class MockWarmupMonitorAdapter:
    def __init__(self, fixture_path: str | Path) -> None:
        self.data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))

    def evaluate_domain(self, domain: str) -> DomainWarmupEvaluation:
        record = self.data.get(domain, {})
        reasons: list[str] = []

        warmup_score = int(record.get("warmup_score", 0))
        bounce_rate_pct = float(record.get("bounce_rate_pct", 100.0))
        daily_send_limit = int(record.get("daily_send_limit", 0))

        if warmup_score < 65:
            reasons.append(f"low_warmup_score:{warmup_score}")
        if bounce_rate_pct > 3.0:
            reasons.append(f"high_bounce_rate:{bounce_rate_pct}")

        if reasons:
            status: RotationStatus = "quarantined"
            recommended_cap = 0
            gate_passed = False
        elif warmup_score < 85 or bounce_rate_pct > 1.5:
            status = "throttled"
            recommended_cap = min(daily_send_limit, 15)
            gate_passed = True
            if warmup_score < 85:
                reasons.append(f"warmup_below_target:{warmup_score}")
            if bounce_rate_pct > 1.5:
                reasons.append(f"elevated_bounce_rate:{bounce_rate_pct}")
        else:
            status = "active"
            recommended_cap = min(daily_send_limit, 30)
            gate_passed = True

        return DomainWarmupEvaluation(
            domain=domain,
            provider="mock_warmup_fixture",
            warmup_score=warmup_score,
            bounce_rate_pct=bounce_rate_pct,
            daily_send_limit=daily_send_limit,
            recommended_daily_cap=recommended_cap,
            rotation_status=status,
            gate_passed=gate_passed,
            reasons=reasons,
        )
