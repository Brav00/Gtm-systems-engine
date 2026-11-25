import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import TypeAdapter

from gtm_engine.adapters.greenhouse_signals import LiveGreenhouseSignalAdapter
from gtm_engine.adapters.http_webhook_notifier import HttpWebhookReadinessNotifier
from gtm_engine.adapters.mock_signals import MockSignalCollectorAdapter
from gtm_engine.adapters.noop_notifier import NoopReadinessNotifier
from gtm_engine.contracts.signals import SignalTargetBoard, normalize_keywords
from gtm_engine.ports.notifications import SignalNotifierPort
from gtm_engine.ports.signals import SignalCollectorPort


_TARGETS = TypeAdapter(list[SignalTargetBoard])


def load_targets(path: str | Path) -> tuple[list[SignalTargetBoard], list[str]]:
    """Read {"keywords": [...], "targets": [...]} or a bare list of targets."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    keywords: list[str] = []
    if isinstance(data, dict):
        raw_keywords = data.get("keywords", [])
        if not isinstance(raw_keywords, list) or not all(isinstance(k, str) for k in raw_keywords):
            raise ValueError("keywords must be a list of strings")
        keywords = raw_keywords
        data = data.get("targets")
    if not isinstance(data, list) or not data:
        raise ValueError("targets file must contain a non-empty list of targets")
    return _TARGETS.validate_python(data), keywords


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect public-data intent signals from company job boards.")
    parser.add_argument(
        "--targets-file",
        default="fixtures/sample_signal_targets.json",
        help="JSON with target companies (and optional default keywords).",
    )
    parser.add_argument(
        "--keywords",
        default=None,
        help='Comma-separated keywords, e.g. "Growth,RevOps,Sales". Overrides the file; empty matches all roles.',
    )
    parser.add_argument(
        "--mode",
        choices=["mock", "live"],
        default="mock",
        help="mock: offline sample postings; live: Greenhouse public job boards.",
    )
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("SIGNAL_WEBHOOK_URL", ""),
        help="Webhook URL to POST the signal report to (default: $SIGNAL_WEBHOOK_URL).",
    )
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if no signals match.")
    args = parser.parse_args()

    try:
        targets, file_keywords = load_targets(args.targets_file)
    except (OSError, ValueError) as exc:
        print(f"error: could not load targets from {args.targets_file}: {exc}", file=sys.stderr)
        return 2

    keywords = normalize_keywords(args.keywords.split(",") if args.keywords is not None else file_keywords)

    collector: SignalCollectorPort = (
        LiveGreenhouseSignalAdapter() if args.mode == "live" else MockSignalCollectorAdapter()
    )
    notifier: SignalNotifierPort = (
        HttpWebhookReadinessNotifier(args.webhook_url) if args.webhook_url.strip() else NoopReadinessNotifier()
    )

    report = collector.collect_signals(targets, keywords)
    # Print first so the JSON report is always in the logs, even if webhook delivery fails.
    print(report.model_dump_json(indent=2), flush=True)

    if report.errors and len(report.errors) >= len(targets):
        print(f"error: every target failed: {'; '.join(report.errors)}", file=sys.stderr)
        return 2

    try:
        notifier.notify_signals(report)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.strict and report.matched_count == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
