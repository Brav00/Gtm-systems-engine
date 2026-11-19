from pathlib import Path
from gtm_engine.adapters.mock_warmup import MockWarmupMonitorAdapter
from gtm_engine.cli.evaluate_warmup import run_warmup_evaluation


def test_warmup_gate_classifies_active_throttled_and_quarantined() -> None:
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "sample_warmup.json"
    adapter = MockWarmupMonitorAdapter(fixture)

    report = run_warmup_evaluation(
        domains=["healthy-outreach.com", "throttled-sender.com", "quarantined-sender.com"],
        monitor=adapter,
    )

    assert report.total_domains == 3
    assert report.active_count == 1
    assert report.throttled_count == 1
    assert report.quarantined_count == 1

    by_domain = {item.domain: item for item in report.results}

    assert by_domain["healthy-outreach.com"].rotation_status == "active"
    assert by_domain["healthy-outreach.com"].recommended_daily_cap == 30
    assert by_domain["healthy-outreach.com"].gate_passed is True

    assert by_domain["throttled-sender.com"].rotation_status == "throttled"
    assert by_domain["throttled-sender.com"].recommended_daily_cap == 15
    assert by_domain["throttled-sender.com"].gate_passed is True

    assert by_domain["quarantined-sender.com"].rotation_status == "quarantined"
    assert by_domain["quarantined-sender.com"].recommended_daily_cap == 0
    assert by_domain["quarantined-sender.com"].gate_passed is False
