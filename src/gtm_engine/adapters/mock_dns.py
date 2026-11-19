import json
from pathlib import Path
from gtm_engine.contracts.deliverability import DomainAuditResult


class MockDeliverabilityAdapter:
    def __init__(self, fixture_path: str | Path) -> None:
        self.data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))

    def audit_domain(self, domain: str, dkim_selector: str, tracking_host: str) -> DomainAuditResult:
        record = self.data.get(domain, {})
        reasons: list[str] = []

        mx_present = bool(record.get("mx_present", False))
        spf_present = bool(record.get("spf_present", False))
        spf_valid_syntax = bool(record.get("spf_valid_syntax", False))
        dkim_selector_found = bool(record.get("dkim_selector_found", False))
        dmarc_present = bool(record.get("dmarc_present", False))
        dmarc_policy = record.get("dmarc_policy", "missing")
        tracking_cname_valid = bool(record.get("tracking_cname_valid", False))

        if not mx_present:
            reasons.append("missing_mx")
        if not spf_present or not spf_valid_syntax:
            reasons.append("invalid_or_missing_spf")
        if not dkim_selector_found:
            reasons.append(f"missing_dkim_selector:{dkim_selector}")
        if not dmarc_present or dmarc_policy == "missing":
            reasons.append("missing_dmarc")
        if not tracking_cname_valid:
            reasons.append(f"invalid_tracking_cname:{tracking_host}")

        return DomainAuditResult(
            domain=domain,
            provider="mock_fixture",
            mx_present=mx_present,
            spf_present=spf_present,
            spf_valid_syntax=spf_valid_syntax,
            dkim_selector_found=dkim_selector_found,
            dmarc_present=dmarc_present,
            dmarc_policy=dmarc_policy,
            tracking_cname_valid=tracking_cname_valid,
            gate_passed=len(reasons) == 0,
            reasons=reasons,
        )
