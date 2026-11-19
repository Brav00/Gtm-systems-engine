from pathlib import Path
from gtm_engine.adapters.mock_dns import MockDeliverabilityAdapter
from gtm_engine.cli.audit_domains import run_audit


def test_mock_deliverability_gate_separates_pass_and_fail() -> None:
    fixture = Path(__file__).resolve().parent.parent / "fixtures" / "sample_domains.json"
    adapter = MockDeliverabilityAdapter(fixture)

    report = run_audit(
        domains=["healthy-outreach.com", "broken-sender.com"],
        auditor=adapter,
        dkim_selector="google",
        tracking_host="track",
    )

    assert report.total_domains == 2
    assert report.passed_count == 1
    assert report.failed_count == 1

    by_domain = {item.domain: item for item in report.results}
    assert by_domain["healthy-outreach.com"].gate_passed is True
    assert by_domain["healthy-outreach.com"].reasons == []

    assert by_domain["broken-sender.com"].gate_passed is False
    assert "invalid_or_missing_spf" in by_domain["broken-sender.com"].reasons
    assert "missing_dkim_selector:google" in by_domain["broken-sender.com"].reasons
    assert "missing_dmarc" in by_domain["broken-sender.com"].reasons
