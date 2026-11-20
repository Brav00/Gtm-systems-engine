from pathlib import Path
from gtm_engine.adapters.mock_dns import MockDeliverabilityAdapter
from gtm_engine.adapters.mock_warmup import MockWarmupMonitorAdapter
from gtm_engine.cli.check_readiness import run_readiness_check


def test_combined_readiness_gate_blocks_when_either_gate_fails() -> None:
    root = Path(__file__).resolve().parent.parent
    dns_adapter = MockDeliverabilityAdapter(root / "fixtures" / "sample_domains.json")
    warmup_adapter = MockWarmupMonitorAdapter(root / "fixtures" / "sample_warmup.json")

    # Use one domain present in sample_domains.json and one quarantined/missing domain
    report = run_readiness_check(
        domains=["healthy-outreach.com", "quarantined-sender.com"],
        dns_auditor=dns_adapter,
        warmup_monitor=warmup_adapter,
    )

    assert report.total_domains == 2
    by_domain = {d.domain: d for d in report.decisions}

    # Quarantined domain must always be blocked with 0 daily capacity
    blocked = by_domain["quarantined-sender.com"]
    assert blocked.ready_to_send is False
    assert blocked.effective_daily_cap == 0
    assert len(blocked.blocking_reasons) >= 1
