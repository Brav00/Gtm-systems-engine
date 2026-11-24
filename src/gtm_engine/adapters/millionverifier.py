import json
import urllib.error
import urllib.parse
import urllib.request

from gtm_engine.contracts.leads import VerificationStatus
from gtm_engine.ports.verifier import VerifierAuthError, VerifierError


_RESULT_MAP: dict[str, VerificationStatus] = {
    "ok": "valid",
    "catch_all": "risky",
    "unknown": "risky",
    "invalid": "invalid",
    "disposable": "invalid",
}


class LiveMillionVerifierAdapter:
    provider_name = "millionverifier"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.millionverifier.com/api/v3/",
        timeout_seconds: float = 20.0,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        self.api_key = api_key.strip()
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "***")

    def verify_email(self, email: str) -> tuple[VerificationStatus, list[str]]:
        query = urllib.parse.urlencode({"api": self.api_key, "email": email})
        req = urllib.request.Request(
            url=f"{self.base_url}?{query}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        # Error messages never include the request URL: it carries the API key.
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise VerifierError(f"MillionVerifier API returned unexpected status: {resp.status}")
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise VerifierAuthError(
                    f"MillionVerifier API rejected the API key (status {exc.code})"
                ) from None
            raise VerifierError(f"MillionVerifier API request failed with status {exc.code}") from None
        except urllib.error.URLError as exc:
            raise VerifierError(f"Failed to reach MillionVerifier API: {self._redact(str(exc.reason))}") from None
        except TimeoutError:
            raise VerifierError(
                f"MillionVerifier API timed out after {self.timeout_seconds:g} seconds"
            ) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise VerifierError("MillionVerifier API returned an invalid JSON response") from None

        if not isinstance(data, dict):
            raise VerifierError("MillionVerifier API returned an unexpected response shape")

        # MillionVerifier reports account problems (bad key, no credits) in an "error" field.
        api_error = str(data.get("error") or "").strip()
        if api_error:
            raise VerifierAuthError(f"MillionVerifier API error: {self._redact(api_error)}")

        result = str(data.get("result") or "").strip().lower()
        status = _RESULT_MAP.get(result, "unknown")

        reasons = [f"result:{result or 'missing'}"]
        subresult = str(data.get("subresult") or "").strip().lower()
        if subresult:
            reasons.append(f"subresult:{subresult}")
        return status, reasons
