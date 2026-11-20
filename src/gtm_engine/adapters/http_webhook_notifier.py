import json
import urllib.error
import urllib.request
from gtm_engine.contracts.readiness import ReadinessBatchReport


class HttpWebhookReadinessNotifier:
    def __init__(self, webhook_url: str, timeout_seconds: float = 10.0) -> None:
        if not webhook_url or not webhook_url.strip():
            raise ValueError("webhook_url must be a non-empty string")
        self.webhook_url = webhook_url.strip()
        self.timeout_seconds = timeout_seconds

    def notify_readiness(self, report: ReadinessBatchReport) -> None:
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
            raise RuntimeError(f"Failed to deliver readiness webhook: {exc}") from exc
