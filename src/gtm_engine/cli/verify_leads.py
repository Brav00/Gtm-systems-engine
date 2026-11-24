import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import TypeAdapter

from gtm_engine.adapters.http_webhook_notifier import HttpWebhookReadinessNotifier
from gtm_engine.adapters.millionverifier import LiveMillionVerifierAdapter
from gtm_engine.adapters.mock_verifier import MockEmailVerifierAdapter
from gtm_engine.adapters.noop_notifier import NoopReadinessNotifier
from gtm_engine.adapters.waterfall_verifier import (
    VerificationAbortedError,
    VerificationWaterfall,
    with_lead_context,
)
from gtm_engine.contracts.leads import LeadInput, LeadVerificationResult, VerificationBatchReport
from gtm_engine.ports.notifications import VerificationNotifierPort
from gtm_engine.ports.verifier import EmailVerifierPort


_LEADS = TypeAdapter(list[LeadInput])


def load_leads(path: str | Path) -> list[LeadInput]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("leads file must contain a JSON list of lead objects")
    return _LEADS.validate_python(data)


def run_verification(leads: list[LeadInput], waterfall: VerificationWaterfall) -> VerificationBatchReport:
    # Duplicate emails in one batch are verified once, so they never spend credits twice;
    # each lead still keeps its own name and company on its result row.
    cache: dict[str, LeadVerificationResult] = {}
    results: list[LeadVerificationResult] = []
    for position, lead in enumerate(leads, start=1):
        if lead.email not in cache:
            try:
                cache[lead.email] = waterfall.verify(lead.email)
            except VerificationAbortedError as exc:
                raise VerificationAbortedError(
                    f"{exc} [stopped at lead {position} of {len(leads)}; no report produced]"
                ) from None
        results.append(with_lead_context(cache[lead.email], lead))

    deliverable_leads: list[LeadVerificationResult] = []
    seen: set[str] = set()
    for result in results:
        if result.deliverable and result.email not in seen:
            seen.add(result.email)
            deliverable_leads.append(result)

    return VerificationBatchReport(
        total_leads=len(results),
        valid_count=sum(1 for r in results if r.status == "valid"),
        risky_count=sum(1 for r in results if r.status == "risky"),
        invalid_count=sum(1 for r in results if r.status == "invalid"),
        unknown_count=sum(1 for r in results if r.status == "unknown"),
        deliverable_emails=[r.email for r in deliverable_leads],
        deliverable_leads=deliverable_leads,
        results=results,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify lead emails through a provider waterfall.")
    parser.add_argument("--leads-file", default="fixtures/sample_leads.json", help="JSON list of leads.")
    parser.add_argument(
        "--mode",
        choices=["mock", "live"],
        default="mock",
        help="mock: offline keyword verifier; live: MillionVerifier API.",
    )
    parser.add_argument(
        "--verifier-api-key",
        default=os.environ.get("VERIFIER_API_KEY", ""),
        help="MillionVerifier API key for --mode live (default: $VERIFIER_API_KEY).",
    )
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("VERIFICATION_WEBHOOK_URL", ""),
        help="Webhook URL to POST the verification report to (default: $VERIFICATION_WEBHOOK_URL).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any lead is not deliverable (invalid, risky or unknown).",
    )
    args = parser.parse_args()

    if args.mode == "live" and not args.verifier_api_key.strip():
        parser.error("--mode live requires --verifier-api-key or VERIFIER_API_KEY")

    try:
        leads = load_leads(args.leads_file)
    except (OSError, ValueError) as exc:
        print(f"error: could not load leads from {args.leads_file}: {exc}", file=sys.stderr)
        return 2

    providers: list[EmailVerifierPort] = (
        [LiveMillionVerifierAdapter(args.verifier_api_key)]
        if args.mode == "live"
        else [
            MockEmailVerifierAdapter(provider_name="mock_primary"),
            MockEmailVerifierAdapter(provider_name="mock_fallback"),
        ]
    )
    notifier: VerificationNotifierPort = (
        HttpWebhookReadinessNotifier(args.webhook_url) if args.webhook_url.strip() else NoopReadinessNotifier()
    )

    try:
        report = run_verification(leads, VerificationWaterfall(providers))
    except VerificationAbortedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Print first so the JSON report is always in the logs, even if webhook delivery fails.
    print(report.model_dump_json(indent=2), flush=True)

    try:
        notifier.notify_verification(report)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.strict and report.valid_count < report.total_leads:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
