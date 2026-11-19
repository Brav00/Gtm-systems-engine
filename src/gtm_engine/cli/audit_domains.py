import argparse
import sys
from gtm_engine.adapters.dns_live import LiveDnsDeliverabilityAdapter
from gtm_engine.adapters.mock_dns import MockDeliverabilityAdapter
from gtm_engine.contracts.deliverability import AuditBatchReport, DomainAuditResult
from gtm_engine.ports.deliverability import DeliverabilityAuditorPort


def run_audit(
    domains: list[str],
    auditor: DeliverabilityAuditorPort,
    dkim_selector: str = "s1",
    tracking_host: str = "trk",
) -> AuditBatchReport:
    results: list[DomainAuditResult] = [
        auditor.audit_domain(domain=d.strip(), dkim_selector=dkim_selector, tracking_host=tracking_host)
        for d in domains
        if d.strip()
    ]
    passed = sum(1 for r in results if r.gate_passed)
    failed = len(results) - passed
    return AuditBatchReport(
        total_domains=len(results),
        passed_count=passed,
        failed_count=failed,
        results=results,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit sending domains for cold email deliverability.")
    parser.add_argument("--domains", required=True, help="Comma-separated list of domains to audit.")
    parser.add_argument("--selector", default="s1", help="DKIM selector (default: s1).")
    parser.add_argument("--tracking-host", default="trk", help="Custom tracking subdomain (default: trk).")
    parser.add_argument("--mode", choices=["mock", "live"], default="mock", help="Use mock fixtures or live DNS.")
    parser.add_argument("--fixture", default="fixtures/sample_domains.json", help="Path to mock JSON fixture.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any domain fails the quality gate.")
    args = parser.parse_args()

    domain_list = [item.strip() for item in args.domains.split(",") if item.strip()]
    auditor: DeliverabilityAuditorPort = (
        MockDeliverabilityAdapter(args.fixture) if args.mode == "mock" else LiveDnsDeliverabilityAdapter()
    )

    report = run_audit(
        domains=domain_list,
        auditor=auditor,
        dkim_selector=args.selector,
        tracking_host=args.tracking_host,
    )
    print(report.model_dump_json(indent=2))

    if args.strict and report.failed_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
