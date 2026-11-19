import argparse
import sys
from gtm_engine.adapters.mock_warmup import MockWarmupMonitorAdapter
from gtm_engine.contracts.warmup import DomainWarmupEvaluation, WarmupBatchReport
from gtm_engine.ports.warmup import WarmupMonitorPort


def run_warmup_evaluation(
    domains: list[str],
    monitor: WarmupMonitorPort,
) -> WarmupBatchReport:
    results: list[DomainWarmupEvaluation] = [
        monitor.evaluate_domain(domain=d.strip())
        for d in domains
        if d.strip()
    ]
    active = sum(1 for r in results if r.rotation_status == "active")
    throttled = sum(1 for r in results if r.rotation_status == "throttled")
    quarantined = sum(1 for r in results if r.rotation_status == "quarantined")

    return WarmupBatchReport(
        total_domains=len(results),
        active_count=active,
        throttled_count=throttled,
        quarantined_count=quarantined,
        results=results,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate domain warm-up health and rotation caps.")
    parser.add_argument("--domains", required=True, help="Comma-separated list of domains to evaluate.")
    parser.add_argument("--fixture", default="fixtures/sample_warmup.json", help="Path to warm-up JSON fixture.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any domain is quarantined.")
    args = parser.parse_args()

    domain_list = [item.strip() for item in args.domains.split(",") if item.strip()]
    monitor: WarmupMonitorPort = MockWarmupMonitorAdapter(args.fixture)

    report = run_warmup_evaluation(domains=domain_list, monitor=monitor)
    print(report.model_dump_json(indent=2))

    if args.strict and report.quarantined_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
