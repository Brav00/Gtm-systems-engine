import argparse
import os
import sys
from pathlib import Path

from gtm_engine.adapters.http_webhook_notifier import HttpWebhookReadinessNotifier
from gtm_engine.adapters.mock_campaign import MockCampaignProvisionerAdapter
from gtm_engine.adapters.noop_notifier import NoopReadinessNotifier
from gtm_engine.adapters.smartlead_campaign import LiveSmartleadCampaignAdapter
from gtm_engine.contracts.campaign import CampaignProvisioningRequest, ProvisioningGuardrailError
from gtm_engine.contracts.leads import VerificationBatchReport
from gtm_engine.contracts.readiness import ReadinessBatchReport
from gtm_engine.ports.campaign import CampaignProvisionerPort
from gtm_engine.ports.notifications import CampaignNotifierPort


def build_provisioning_request(
    campaign_name: str,
    readiness: ReadinessBatchReport,
    verification: VerificationBatchReport,
    campaign_id: int | None = None,
    activate_campaign: bool = False,
) -> CampaignProvisioningRequest:
    """Turn the Day 1 and Day 2 reports into a provisioning request.

    Only domains that are ready_to_send with a daily cap > 0 are allowed; the rest are excluded.
    All Day 2 results are passed through; the provisioner's guardrails strip anything not valid.
    """
    allowed: list[str] = []
    excluded: list[str] = []
    caps: dict[str, int] = {}
    for decision in readiness.decisions:
        domain = decision.domain.strip().lower()
        if (
            decision.ready_to_send
            and decision.effective_daily_cap > 0
            and decision.warmup_report.recommended_daily_cap > 0
        ):
            allowed.append(domain)
            caps[domain] = decision.effective_daily_cap
        else:
            excluded.append(domain)

    return CampaignProvisioningRequest(
        campaign_name=campaign_name,
        campaign_id=campaign_id,
        allowed_domains=allowed,
        excluded_domains=excluded,
        domain_daily_caps=caps,
        leads=verification.results,
        activate_campaign=activate_campaign,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision a cold-email campaign from the Day 1 readiness and Day 2 verification reports."
    )
    parser.add_argument("--campaign-name", required=True, help="Name for a new campaign.")
    parser.add_argument("--campaign-id", type=int, default=None, help="Existing campaign ID to add to (optional).")
    parser.add_argument("--readiness-report", required=True, help="Path to the Day 1 readiness report JSON.")
    parser.add_argument("--verification-report", required=True, help="Path to the Day 2 verification report JSON.")
    parser.add_argument(
        "--mode",
        choices=["mock", "live"],
        default="mock",
        help="mock: offline simulation; live: Smartlead API.",
    )
    parser.add_argument(
        "--smartlead-api-key",
        default=os.environ.get("SMARTLEAD_API_KEY", ""),
        help="Smartlead API key for --mode live (default: $SMARTLEAD_API_KEY).",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Start the campaign after provisioning. Without it the campaign stays drafted/paused.",
    )
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("CAMPAIGN_WEBHOOK_URL", ""),
        help="Webhook URL to POST the provisioning report to (default: $CAMPAIGN_WEBHOOK_URL).",
    )
    args = parser.parse_args()

    if args.mode == "live" and not args.smartlead_api_key.strip():
        parser.error("--mode live requires --smartlead-api-key or SMARTLEAD_API_KEY")

    try:
        readiness = ReadinessBatchReport.model_validate_json(Path(args.readiness_report).read_text(encoding="utf-8"))
        verification = VerificationBatchReport.model_validate_json(
            Path(args.verification_report).read_text(encoding="utf-8")
        )
        request = build_provisioning_request(
            campaign_name=args.campaign_name,
            readiness=readiness,
            verification=verification,
            campaign_id=args.campaign_id,
            activate_campaign=args.activate,
        )
    except (OSError, ValueError) as exc:
        print(f"error: could not load reports: {exc}", file=sys.stderr)
        return 2

    provisioner: CampaignProvisionerPort = (
        LiveSmartleadCampaignAdapter(args.smartlead_api_key)
        if args.mode == "live"
        else MockCampaignProvisionerAdapter()
    )
    notifier: CampaignNotifierPort = (
        HttpWebhookReadinessNotifier(args.webhook_url) if args.webhook_url.strip() else NoopReadinessNotifier()
    )

    try:
        report = provisioner.provision_campaign(request)
    except ProvisioningGuardrailError as exc:
        print(f"blocked: guardrail {exc.code}; no campaign changes were made", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Print first so the JSON report is always in the logs, even if webhook delivery fails.
    print(report.model_dump_json(indent=2), flush=True)

    try:
        notifier.notify_campaign(report)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
