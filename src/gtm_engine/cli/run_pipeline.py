import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from gtm_engine.adapters.dns_live import LiveDnsDeliverabilityAdapter
from gtm_engine.adapters.http_webhook_notifier import HttpWebhookReadinessNotifier
from gtm_engine.adapters.lead_sources import (
    FileLeadSourceAdapter,
    GoogleSheetCsvLeadSourceAdapter,
    MockLeadSourceAdapter,
)
from gtm_engine.adapters.millionverifier import LiveMillionVerifierAdapter
from gtm_engine.adapters.mock_campaign import MockCampaignProvisionerAdapter
from gtm_engine.adapters.mock_dns import MockDeliverabilityAdapter
from gtm_engine.adapters.mock_verifier import MockEmailVerifierAdapter
from gtm_engine.adapters.mock_warmup import MockWarmupMonitorAdapter
from gtm_engine.adapters.noop_notifier import NoopReadinessNotifier
from gtm_engine.adapters.smartlead_campaign import LiveSmartleadCampaignAdapter
from gtm_engine.adapters.smartlead_warmup import SmartleadWarmupAdapter
from gtm_engine.adapters.waterfall_verifier import VerificationWaterfall
from gtm_engine.cli.check_readiness import run_readiness_check
from gtm_engine.cli.provision_campaign import build_provisioning_request
from gtm_engine.cli.verify_leads import run_verification
from gtm_engine.contracts.campaign import ProvisioningGuardrailError
from gtm_engine.contracts.pipeline import PipelineRunSummary
from gtm_engine.ports.campaign import CampaignProvisionerPort
from gtm_engine.ports.deliverability import DeliverabilityAuditorPort
from gtm_engine.ports.lead_source import LeadSourcePort
from gtm_engine.ports.notifications import PipelineNotifierPort
from gtm_engine.ports.verifier import EmailVerifierPort
from gtm_engine.ports.warmup import WarmupMonitorPort


EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_ERROR = 2


@dataclass
class PipelineDependencies:
    lead_source: LeadSourcePort
    waterfall: VerificationWaterfall
    dns_auditor: DeliverabilityAuditorPort
    warmup_monitor: WarmupMonitorPort
    provisioner: CampaignProvisionerPort


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "campaign"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def create_run_dir(base: Path, now: datetime) -> tuple[str, Path]:
    """Create base/<UTC timestamp>, adding -2, -3, ... so an earlier run is never overwritten."""
    base.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for attempt in range(1, 1000):
        run_id = stamp if attempt == 1 else f"{stamp}-{attempt}"
        run_dir = base / run_id
        try:
            run_dir.mkdir()
        except FileExistsError:
            continue
        return run_id, run_dir
    raise OSError(f"could not create a unique run directory under {base}")


def _write_artifact(
    summary: PipelineRunSummary, run_dir: Path, key: str, filename: str, payload: BaseModel | Sequence[BaseModel]
) -> None:
    path = run_dir / filename
    if isinstance(payload, BaseModel):
        text = payload.model_dump_json(indent=2)
    else:
        text = json.dumps([item.model_dump(mode="json") for item in payload], indent=2)
    path.write_text(text + "\n", encoding="utf-8")
    summary.artifacts[key] = str(path)


def execute_pipeline(
    deps: PipelineDependencies,
    summary: PipelineRunSummary,
    run_dir: Path,
    *,
    source_ref: str,
    domains: list[str],
    selector: str = "google",
    tracking_host: str = "mail",
) -> PipelineRunSummary:
    """Run all four stages in order, saving each stage's JSON, and set status / exit_code on the summary."""
    stage = "load_leads"
    try:
        # 1. Lead source.
        leads = deps.lead_source.fetch_leads(source_ref)
        summary.leads_loaded = len(leads)
        _write_artifact(summary, run_dir, "raw_leads", "01_raw_leads.json", leads)

        # 2. Day 2 verification waterfall (deduplicates and verifies).
        stage = "verify_leads"
        verification = run_verification(leads, deps.waterfall)
        summary.valid_leads = verification.valid_count
        summary.risky_leads = verification.risky_count
        summary.invalid_leads = verification.invalid_count
        summary.unknown_leads = verification.unknown_count
        _write_artifact(summary, run_dir, "verification", "02_verification_report.json", verification)

        # 3. Day 1 deliverability readiness gate.
        stage = "readiness_gate"
        readiness = run_readiness_check(
            domains=domains,
            dns_auditor=deps.dns_auditor,
            warmup_monitor=deps.warmup_monitor,
            selector=selector,
            tracking_host=tracking_host,
        )
        _write_artifact(summary, run_dir, "readiness", "03_readiness_report.json", readiness)

        # 4. Day 3 campaign provisioning: ready domains and valid leads only.
        stage = "provision_campaign"
        request = build_provisioning_request(
            campaign_name=summary.campaign_name,
            readiness=readiness,
            verification=verification,
            campaign_id=summary.campaign_id,
            activate_campaign=summary.launch_requested,
        )
        summary.ready_domains = list(request.allowed_domains)
        summary.blocked_domains = list(request.excluded_domains)

        blocks: list[str] = []
        if not request.allowed_domains:
            blocks.append("guardrail:no_ready_domains_available")
        if verification.valid_count == 0:
            blocks.append("guardrail:no_valid_leads_to_upload")
        if blocks:
            summary.reasons.extend(blocks)
            for decision in readiness.decisions:
                if decision.domain.strip().lower() in request.excluded_domains:
                    summary.reasons.extend(f"domain_blocked:{decision.domain}:{r}" for r in decision.blocking_reasons)
            if verification.valid_count == 0:
                summary.reasons.append(
                    f"lead_counts:loaded={summary.leads_loaded},risky={summary.risky_leads},"
                    f"invalid={summary.invalid_leads},unknown={summary.unknown_leads}"
                )
            summary.reasons.append("no_campaign_changes_made")
            summary.status, summary.exit_code = "blocked", EXIT_BLOCKED
            return summary

        report = deps.provisioner.provision_campaign(request)
        _write_artifact(summary, run_dir, "provisioning", "04_provisioning_report.json", report)
        summary.campaign_id = report.campaign_id
        summary.campaign_status = report.campaign_status
        summary.uploaded_leads = report.uploaded_leads_count
        summary.total_daily_capacity = report.total_daily_capacity
        summary.reasons.extend(report.reasons)
        summary.status, summary.exit_code = "provisioned", EXIT_OK

    except ProvisioningGuardrailError as exc:
        summary.reasons.extend([f"guardrail:{exc.code}", "no_campaign_changes_made"])
        summary.status, summary.exit_code = "blocked", EXIT_BLOCKED
    except (RuntimeError, OSError) as exc:
        # Adapter errors are already masked: no API keys, tokens or full URLs.
        summary.reasons.append(f"{stage}_failed:{exc}")
        summary.status, summary.exit_code = "error", EXIT_ERROR
    return summary


def build_dependencies(args: argparse.Namespace) -> PipelineDependencies:
    lead_source: LeadSourcePort
    if args.source_type == "file":
        lead_source = FileLeadSourceAdapter()
    elif args.source_type == "gsheet":
        lead_source = GoogleSheetCsvLeadSourceAdapter()
    else:
        lead_source = MockLeadSourceAdapter()

    if args.mode == "live":
        verifiers: list[EmailVerifierPort] = [LiveMillionVerifierAdapter(args.verifier_api_key)]
        return PipelineDependencies(
            lead_source=lead_source,
            waterfall=VerificationWaterfall(verifiers),
            dns_auditor=LiveDnsDeliverabilityAdapter(),
            warmup_monitor=SmartleadWarmupAdapter(args.smartlead_api_key),
            provisioner=LiveSmartleadCampaignAdapter(args.smartlead_api_key),
        )
    return PipelineDependencies(
        lead_source=lead_source,
        waterfall=VerificationWaterfall(
            [MockEmailVerifierAdapter("mock_primary"), MockEmailVerifierAdapter("mock_fallback")]
        ),
        dns_auditor=MockDeliverabilityAdapter(args.dns_fixture),
        warmup_monitor=MockWarmupMonitorAdapter(args.warmup_fixture),
        provisioner=MockCampaignProvisionerAdapter(),
    )


def _parse_args(argv: Sequence[str] | None) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(
        description="Master GTM pipeline: lead source -> verification -> readiness gate -> campaign provisioning."
    )
    parser.add_argument("--source-type", choices=["mock", "file", "gsheet"], required=True, help="Where leads come from.")
    parser.add_argument("--source-ref", default=None, help="File path (.csv/.json) or Google Sheet URL/ID.")
    parser.add_argument("--domains", required=True, help="Comma-separated sending domains for the readiness gate.")
    parser.add_argument("--campaign-name", required=True, help="Campaign name.")
    parser.add_argument("--campaign-id", type=int, default=None, help="Existing campaign ID to reuse (optional).")
    parser.add_argument("--mode", choices=["mock", "live"], default="mock", help="mock: offline; live: real APIs.")
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Start sending after provisioning. Without it the campaign stays drafted/paused.",
    )
    parser.add_argument("--output-dir", default=None, help="Base folder for run artifacts (default: runs/<campaign-slug>).")
    parser.add_argument(
        "--verifier-api-key",
        default=os.environ.get("VERIFIER_API_KEY") or os.environ.get("MILLIONVERIFIER_API_KEY", ""),
        help="MillionVerifier key for --mode live (default: $VERIFIER_API_KEY or $MILLIONVERIFIER_API_KEY).",
    )
    parser.add_argument(
        "--smartlead-api-key",
        default=os.environ.get("SMARTLEAD_API_KEY", ""),
        help="Smartlead key for --mode live (default: $SMARTLEAD_API_KEY).",
    )
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("PIPELINE_WEBHOOK_URL", ""),
        help="Webhook URL to POST the run summary to (default: $PIPELINE_WEBHOOK_URL).",
    )
    parser.add_argument("--selector", default="google", help="DKIM selector for the readiness gate.")
    parser.add_argument("--tracking-host", default="mail", help="Tracking subdomain for the readiness gate.")
    parser.add_argument("--dns-fixture", default="fixtures/sample_domains.json", help="Mock DNS fixture.")
    parser.add_argument("--warmup-fixture", default="fixtures/sample_warmup.json", help="Mock warm-up fixture.")
    args = parser.parse_args(argv)

    args.campaign_name = args.campaign_name.strip()
    args.domain_list = [d.strip() for d in args.domains.split(",") if d.strip()]
    if not args.campaign_name:
        parser.error("--campaign-name must not be blank")
    if args.campaign_id is not None and args.campaign_id < 1:
        parser.error("--campaign-id must be a positive integer")
    if not args.domain_list:
        parser.error("--domains needs at least one sending domain")
    if args.source_type != "mock" and not (args.source_ref or "").strip():
        parser.error(f"--source-type {args.source_type} requires --source-ref")
    if args.mode == "live":
        missing = []
        if not args.verifier_api_key.strip():
            missing.append("--verifier-api-key (or VERIFIER_API_KEY / MILLIONVERIFIER_API_KEY)")
        if not args.smartlead_api_key.strip():
            missing.append("--smartlead-api-key (or SMARTLEAD_API_KEY)")
        if missing:
            parser.error("--mode live requires " + " and ".join(missing))
    return parser, args


def main(argv: Sequence[str] | None = None) -> int:
    _, args = _parse_args(argv)

    started = _utc_now()
    base_dir = Path(args.output_dir) if args.output_dir else Path("runs") / slugify(args.campaign_name)
    try:
        deps = build_dependencies(args)
        run_id, run_dir = create_run_dir(base_dir, started)
    except (OSError, ValueError) as exc:
        print(f"error: could not set up the pipeline: {exc}", file=sys.stderr)
        return EXIT_ERROR

    summary = PipelineRunSummary(
        run_id=run_id,
        status="error",
        exit_code=EXIT_ERROR,
        mode=args.mode,
        source_type=args.source_type,
        campaign_name=args.campaign_name,
        campaign_id=args.campaign_id,
        launch_requested=args.launch,
        domains_checked=args.domain_list,
        output_dir=str(run_dir),
        started_at=started.isoformat(),
    )
    execute_pipeline(
        deps,
        summary,
        run_dir,
        source_ref=(args.source_ref or "").strip(),
        domains=args.domain_list,
        selector=args.selector,
        tracking_host=args.tracking_host,
    )
    summary.finished_at = _utc_now().isoformat()
    summary.artifacts["run_summary"] = str(run_dir / "run_summary.json")

    # Persist and print the summary for every outcome, including blocks and errors.
    try:
        (run_dir / "run_summary.json").write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"error: could not save run summary: {exc}", file=sys.stderr)
        summary.exit_code = EXIT_ERROR
    print(summary.model_dump_json(indent=2), flush=True)

    if summary.status == "blocked":
        print("blocked: " + "; ".join(r for r in summary.reasons if r.startswith("guardrail:")), file=sys.stderr)
    elif summary.status == "error":
        print("error: " + "; ".join(r for r in summary.reasons if "_failed:" in r), file=sys.stderr)

    if summary.status != "error":
        notifier: PipelineNotifierPort = (
            HttpWebhookReadinessNotifier(args.webhook_url) if args.webhook_url.strip() else NoopReadinessNotifier()
        )
        try:
            notifier.notify_pipeline(summary)
        except RuntimeError as exc:
            note = " (the campaign was provisioned; do not re-run without --campaign-id)" if summary.status == "provisioned" else ""
            print(f"error: {exc}{note}", file=sys.stderr)
            return EXIT_ERROR
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
