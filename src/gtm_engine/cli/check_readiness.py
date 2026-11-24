import argparse
import os
import sys
from gtm_engine.adapters.dns_live import LiveDnsDeliverabilityAdapter
from gtm_engine.adapters.http_webhook_notifier import HttpWebhookReadinessNotifier
from gtm_engine.adapters.mock_dns import MockDeliverabilityAdapter
from gtm_engine.adapters.mock_warmup import MockWarmupMonitorAdapter
from gtm_engine.adapters.noop_notifier import NoopReadinessNotifier
from gtm_engine.adapters.smartlead_warmup import SmartleadWarmupAdapter
from gtm_engine.contracts.readiness import DomainReadinessDecision, ReadinessBatchReport
from gtm_engine.ports.deliverability import DeliverabilityAuditorPort
from gtm_engine.ports.notifications import ReadinessNotifierPort
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


def dispatch_readiness_notification(
    report: ReadinessBatchReport,
    notifier: ReadinessNotifierPort,
    only_blocked: bool = False,
) -> bool:
    """Send the report via the notifier unless only_blocked is set and nothing is blocked.

    Returns True if the notifier was called.
    """
    if only_blocked and report.blocked_count == 0:
        return False
    notifier.notify_readiness(report)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Combined DNS + Warm-Up pre-flight readiness gate.")
    parser.add_argument("--domains", required=True, help="Comma-separated list of sending domains.")
    parser.add_argument("--dns-mode", choices=["mock", "live"], default="mock", help="DNS check mode.")
    parser.add_argument("--dns-fixture", default="fixtures/sample_domains.json", help="Path to DNS mock fixture.")
    parser.add_argument("--warmup-fixture", default="fixtures/sample_warmup.json", help="Path to warm-up mock fixture.")
    parser.add_argument(
        "--warmup-mode",
        choices=["mock", "live"],
        default="mock",
        help="Warm-up check mode: mock fixture or live Smartlead API.",
    )
    parser.add_argument(
        "--smartlead-api-key",
        default=os.environ.get("SMARTLEAD_API_KEY", ""),
        help="Smartlead API key for --warmup-mode live (default: $SMARTLEAD_API_KEY).",
    )
    parser.add_argument("--selector", default="google", help="DKIM selector.")
    parser.add_argument("--tracking-host", default="mail", help="Tracking subdomain.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any domain is blocked.")
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("READINESS_WEBHOOK_URL", ""),
        help="Webhook URL to POST the readiness report to (default: $READINESS_WEBHOOK_URL).",
    )
    parser.add_argument(
        "--only-blocked",
        action="store_true",
        help="Only send the webhook when at least one domain is blocked.",
    )
    args = parser.parse_args()

    if args.warmup_mode == "live" and not args.smartlead_api_key.strip():
        parser.error("--warmup-mode live requires --smartlead-api-key or SMARTLEAD_API_KEY")

    domain_list = [d.strip() for d in args.domains.split(",") if d.strip()]

    dns_auditor: DeliverabilityAuditorPort = (
        LiveDnsDeliverabilityAdapter()
        if args.dns_mode == "live"
        else MockDeliverabilityAdapter(args.dns_fixture)
    )
    warmup_monitor: WarmupMonitorPort = (
        SmartleadWarmupAdapter(args.smartlead_api_key)
        if args.warmup_mode == "live"
        else MockWarmupMonitorAdapter(args.warmup_fixture)
    )
    notifier: ReadinessNotifierPort = (
        HttpWebhookReadinessNotifier(args.webhook_url) if args.webhook_url.strip() else NoopReadinessNotifier()
    )

    try:
        report = run_readiness_check(
            domains=domain_list,
            dns_auditor=dns_auditor,
            warmup_monitor=warmup_monitor,
            selector=args.selector,
            tracking_host=args.tracking_host,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Print first so the JSON report is always in the logs, even if webhook delivery fails.
    print(report.model_dump_json(indent=2), flush=True)

    try:
        dispatch_readiness_notification(report, notifier, only_blocked=args.only_blocked)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.strict and report.blocked_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
