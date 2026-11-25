import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone

from gtm_engine.contracts.signals import JobPosting, SignalBatchReport, SignalTargetBoard, build_signal_report


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class LiveGreenhouseSignalAdapter:
    """Reads Greenhouse's public job board API. No API key: every board is public data."""

    provider_name = "greenhouse"

    def __init__(
        self,
        base_url: str = "https://boards-api.greenhouse.io/v1/boards",
        timeout_seconds: float = 15.0,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.clock = clock

    def _fetch_jobs(self, target: SignalTargetBoard) -> list[JobPosting]:
        token = target.board_token
        url = f"{self.base_url}/{urllib.parse.quote(token, safe='')}/jobs"
        req = urllib.request.Request(url=url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            if resp.status < 200 or resp.status >= 300:
                raise urllib.error.HTTPError(url, resp.status, "unexpected status", None, None)  # type: ignore[arg-type]
            data = json.loads(resp.read().decode("utf-8"))

        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        postings: list[JobPosting] = []
        for job in jobs:
            if not isinstance(job, dict) or not str(job.get("title") or "").strip():
                continue
            location = job.get("location")
            location_name = str(location.get("name") or "").strip() if isinstance(location, dict) else ""
            evidence_url = str(job.get("absolute_url") or "").strip() or (
                f"https://boards.greenhouse.io/{token}/jobs/{job.get('id')}"
            )
            postings.append(
                JobPosting(
                    target=target,
                    title=str(job["title"]),
                    location=location_name or None,
                    evidence_url=evidence_url,
                )
            )
        return postings

    def collect_signals(self, targets: list[SignalTargetBoard], keywords: list[str]) -> SignalBatchReport:
        postings: list[JobPosting] = []
        errors: list[str] = []
        for target in targets:
            prefix = f"{self.provider_name}:{target.board_token}"
            # One broken board is recorded and skipped; the rest of the batch still runs.
            try:
                postings.extend(self._fetch_jobs(target))
            except urllib.error.HTTPError as exc:
                errors.append(f"{prefix}:http_{exc.code}")
            except urllib.error.URLError as exc:
                errors.append(f"{prefix}:unreachable:{exc.reason}")
            except TimeoutError:
                errors.append(f"{prefix}:timeout")
            except (json.JSONDecodeError, UnicodeDecodeError):
                errors.append(f"{prefix}:invalid_json")
        return build_signal_report(self.provider_name, postings, keywords, detected_at=self.clock(), errors=errors)
