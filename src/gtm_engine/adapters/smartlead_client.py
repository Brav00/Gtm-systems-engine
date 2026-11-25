import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class SmartleadApiError(RuntimeError):
    """A Smartlead API call failed. Messages name the method and path, never the URL or API key."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SmartleadAuthError(SmartleadApiError):
    """Smartlead rejected the API key (401/403)."""


class SmartleadClient:
    PAGE_SIZE = 100
    # Safety stop (50,000 accounts) in case the API ignores offset and keeps returning full pages.
    MAX_PAGES = 500

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://server.smartlead.ai/api/v1",
        timeout_seconds: float = 15.0,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "***")

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        query = urllib.parse.urlencode({**(params or {}), "api_key": self.api_key})
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url=f"{self.base_url}{path}?{query}", data=data, headers=headers, method=method
        )
        where = f"{method} {path}"

        # Never include the request URL in errors: it carries the API key.
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise SmartleadApiError(f"Smartlead API returned status {resp.status} on {where}", resp.status)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SmartleadAuthError(
                    f"Smartlead API rejected the API key (status {exc.code}) on {where}", exc.code
                ) from None
            raise SmartleadApiError(f"Smartlead API request failed with status {exc.code} on {where}", exc.code) from None
        except urllib.error.URLError as exc:
            raise SmartleadApiError(f"Failed to reach Smartlead API on {where}: {self._redact(str(exc.reason))}") from None
        except TimeoutError:
            raise SmartleadApiError(
                f"Smartlead API timed out after {self.timeout_seconds:g} seconds on {where}"
            ) from None

        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise SmartleadApiError(f"Smartlead API returned invalid JSON on {where}") from None

    def list_email_accounts(self) -> list[dict]:
        accounts: list[dict] = []
        offset = 0
        for _ in range(self.MAX_PAGES):
            data = self.request("GET", "/email-accounts", params={"offset": offset, "limit": self.PAGE_SIZE})
            if isinstance(data, dict):
                data = data.get("data", data.get("email_accounts", []))
            page = [acc for acc in data if isinstance(acc, dict)] if isinstance(data, list) else []
            accounts.extend(page)
            if len(page) < self.PAGE_SIZE:
                return accounts
            offset += self.PAGE_SIZE
        # Fail loudly rather than return a truncated list.
        raise SmartleadApiError(
            f"Smartlead API pagination exceeded {self.MAX_PAGES} pages of {self.PAGE_SIZE} accounts"
        )
