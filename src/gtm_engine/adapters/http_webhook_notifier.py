import json
import urllib.error
import urllib.request
from pydantic import BaseModel
from gtm_engine.contracts.campaign import CampaignProvisioningReport
from gtm_engine.contracts.leads import VerificationBatchReport
from gtm_engine.contracts.readiness import ReadinessBatchReport


class HttpWebhookReadinessNotifier:
    def __init__(self, webhook_url: str, timeout_seconds: float = 10.0) -> None:
        if not webhook_url or not webhook_url.strip():
            raise ValueError("webhook_url must be a non-empty string")
        self.webhook_url = webhook_url.strip()
        self.timeout_seconds = timeout_seconds

    def post_payload(self, report: BaseModel, label: str = "readiness") -> None:
        """POST any Pydantic report as JSON to the configured webhook."""
        payload = report.model_dump(mode="json")
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=self.webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise RuntimeError(f"Webhook returned unexpected status: {resp.status}")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Failed to deliver {label} webhook: {exc}") from exc

    def notify_readiness(self, report: ReadinessBatchReport) -> None:
        self.post_payload(report, label="readiness")

    def notify_verification(self, report: VerificationBatchReport) -> None:
        self.post_payload(report, label="verification")

    def notify_campaign(self, report: CampaignProvisioningReport) -> None:
        self.post_payload(report, label="campaign provisioning")
