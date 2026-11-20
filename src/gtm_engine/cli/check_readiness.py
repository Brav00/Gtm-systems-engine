import argparse
import sys
from gtm_engine.adapters.dns_live import LiveDnsDeliverabilityAdapter
from gtm_engine.adapters.mock_dns import MockDeliverabilityAdapter
from gtm_engine.adapters.mock_warmup import MockWarmupMonitorAdapter
from gtm_engine.contracts.readiness import DomainReadinessDecision, ReadinessBatchReport
from gtm_engine.ports.deliverability import DeliverabilityAuditorPort
from gtm_engine.ports.warmup import WarmupMonitorPort


def run_readiness_check(
    domains: list[str],
    dns_auditor: DeliverabilityAuditorPort,
    warmup_monitor: WarmupMonitorPort,
    selector: str = "google",
    tracking_host: str = "mail",
) -> ReadinessBatchReport:
    decisions: list[DomainReadinessDecision] = []

    for raw_domain in domains:
        domain = raw_domain.strip()
        if not domain:
            continue

        dns_res = dns_auditor.audit_domain(
            domain=domain,
            dkim_selector=selector,
            tracking_host=tracking_host,
        )
        warmup_res = warmup_monitor.evaluate_domain(domain=domain)

        blocking_reasons: list[str] = []
        if not dns_res.gate_passed:
            blocking_reasons.extend([f"dns:{r}" for r in dns_res.reasons])
        if not warmup_res.gate_passed:
            blocking_reasons.extend([f"warmup:{r}" for r in warmup_res.reasons])

        ready = dns_res.gate_passed and warmup_res.gate_passed
        effective_cap = warmup_res.recommended_daily_cap if ready else 0

        decisions.append(
            DomainReadinessDecision(
                domain=domain,
                ready_to_send=ready,
                rotation_status=warmup_res.rotation_status,
                effective_daily_cap=effective_cap,
                blocking_reasons=blocking_reasons,
                dns_report=dns_res,
                warmup_report=warmup_res,
            )
        )

    ready_count = sum(1 for d in decisions if d.ready_to_send)
    blocked_count = len(decisions) - ready_count
    total_capacity = sum(d.effective_daily_cap for d in decisions)

    return ReadinessBatchReport(
        total_domains=len(decisions),
        ready_count=ready_count,
        blocked_count=blocked_count,
        total_daily_capacity=total_capacity,
        decisions=decisions,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Combined DNS + Warm-Up pre-flight readiness gate.")
    parser.add_argument("--domains", required=True, help="Comma-separated list of sending domains.")
    parser.add_argument("--dns-mode", choices=["mock", "live"], default="mock", help="DNS check mode.")
    parser.add_argument("--dns-fixture", default="fixtures/sample_domains.json", help="Path to DNS mock fixture.")
    parser.add_argument("--warmup-fixture", default="fixtures/sample_warmup.json", help="Path to warm-up mock fixture.")
    parser.add_argument("--selector", default="google", help="DKIM selector.")
    parser.add_argument("--tracking-host", default="mail", help="Tracking subdomain.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any domain is blocked.")
    args = parser.parse_args()

    domain_list = [d.strip() for d in args.domains.split(",") if d.strip()]

    dns_auditor: DeliverabilityAuditorPort = (
        LiveDnsDeliverabilityAdapter()
        if args.dns_mode == "live"
        else MockDeliverabilityAdapter(args.dns_fixture)
    )
    warmup_monitor: WarmupMonitorPort = MockWarmupMonitorAdapter(args.warmup_fixture)

    report = run_readiness_check(
        domains=domain_list,
        dns_auditor=dns_auditor,
        warmup_monitor=warmup_monitor,
        selector=args.selector,
        tracking_host=args.tracking_host,
    )
    print(report.model_dump_json(indent=2))

    if args.strict and report.blocked_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
