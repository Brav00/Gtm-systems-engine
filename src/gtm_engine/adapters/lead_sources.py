import csv
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gtm_engine.contracts.lead_source import LeadInput
from gtm_engine.ports.lead_source import LeadSourceError


# Normalized header -> standard field. Headers are lowercased and non-alphanumerics become "_",
# so "Work Email", "work-email" and "WORK_EMAIL" all become "work_email".
# For each field, earlier aliases win when a row has several non-empty matches.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "email": ("email", "work_email", "email_address", "work_email_address", "person_email", "contact_email", "e_mail"),
    "first_name": ("first_name", "firstname", "first"),
    "last_name": ("last_name", "lastname", "last", "surname"),
    "company_domain": ("company_domain", "domain", "company_website", "website"),
    "company_name": ("company_name", "company", "organization", "organization_name", "account_name"),
    "signal_hook": ("signal_hook", "hook", "intent_hook", "why_now"),
}
_ALIAS_TO_FIELD = {alias: field for field, aliases in _FIELD_ALIASES.items() for alias in aliases}

MAX_SOURCE_BYTES = 20 * 1024 * 1024


def normalize_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def clean_domain(value: str) -> str:
    """Turn 'https://www.Acme.com/about' into 'acme.com'."""
    domain = value.strip().lower()
    domain = re.sub(r"^[a-z][a-z0-9+.-]*://", "", domain)
    domain = domain.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return domain.removeprefix("www.")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())


def row_to_lead(row: Mapping[str, Any]) -> LeadInput:
    """Map one row with any common header spelling onto LeadInput. Unmapped columns go to `extra`."""
    fields: dict[str, str] = {}
    priority: dict[str, int] = {}
    extra: dict[str, Any] = {}
    for raw_key, raw_value in row.items():
        if raw_key is None:
            continue  # csv puts surplus cells under a None key
        key = normalize_header(str(raw_key))
        value = _text(raw_value)
        field = _ALIAS_TO_FIELD.get(key)
        if field is None:
            if value:
                extra[str(raw_key).strip()] = value
            continue
        rank = _FIELD_ALIASES[field].index(key)
        if value and (field not in fields or rank < priority[field]):
            fields[field] = value
            priority[field] = rank

    if "company_domain" in fields:
        fields["company_domain"] = clean_domain(fields["company_domain"])
    # A row without an email still becomes a lead; verification marks it invalid, so it stays visible.
    return LeadInput(email=fields.pop("email", ""), extra=extra, **fields)


def parse_csv_text(text: str, source_label: str) -> list[LeadInput]:
    reader = csv.DictReader(io.StringIO(text))
    headers = [normalize_header(h) for h in (reader.fieldnames or []) if h]
    if not headers:
        raise LeadSourceError(f"{source_label} has no header row")
    if not any(_ALIAS_TO_FIELD.get(h) == "email" for h in headers):
        raise LeadSourceError(
            f"{source_label} has no email column (looked for: {', '.join(_FIELD_ALIASES['email'])})"
        )
    try:
        return [row_to_lead(row) for row in reader]
    except csv.Error as exc:
        raise LeadSourceError(f"{source_label} is not valid CSV: {exc}") from None


def parse_json_leads(data: Any, source_label: str) -> list[LeadInput]:
    if isinstance(data, dict):
        data = data.get("leads")
    if not isinstance(data, list):
        raise LeadSourceError(f'{source_label} must contain a JSON list of leads or {{"leads": [...]}}')
    leads: list[LeadInput] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise LeadSourceError(f"{source_label}: lead {index} is not a JSON object")
        leads.append(row_to_lead(item))
    return leads


class MockLeadSourceAdapter:
    """Deterministic in-memory leads covering every verification outcome."""

    source_name = "mock"

    SAMPLE_LEADS: tuple[dict[str, str], ...] = (
        {"email": "jane.doe@acme-analytics.com", "first_name": "Jane", "last_name": "Doe",
         "company_domain": "acme-analytics.com", "company_name": "Acme Analytics",
         "signal_hook": "Hiring for Senior RevOps Manager — indicates investment in revenue operations"},
        {"email": "  Sam.Lee@Northwind-Logistics.COM ", "first_name": "Sam", "last_name": "Lee",
         "company_domain": "northwind-logistics.com", "company_name": "Northwind Logistics",
         "signal_hook": "Hiring for Enterprise Account Executive — indicates active sales team expansion"},
        {"email": "bounce.back@globex-health.com", "first_name": "Bo", "company_domain": "globex-health.com",
         "company_name": "Globex Health"},
        {"email": "ops@catchall-agency.com", "company_domain": "catchall-agency.com", "company_name": "Catchall Agency"},
        {"email": "unknown.person@quiet-startup.com", "first_name": "Alex", "company_domain": "quiet-startup.com"},
        {"email": "not-an-email", "company_name": "Typo Co"},
        {"email": "JANE.DOE@acme-analytics.com", "first_name": "Jane", "last_name": "Doe",
         "company_domain": "acme-analytics.com", "company_name": "Acme Analytics"},
    )

    def fetch_leads(self, source_ref: str = "") -> list[LeadInput]:
        return [row_to_lead(row) for row in self.SAMPLE_LEADS]


class FileLeadSourceAdapter:
    """Reads .csv exports (Clay, Cargo, CRMs) or .json lead files."""

    source_name = "file"

    def fetch_leads(self, source_ref: str) -> list[LeadInput]:
        path = Path(source_ref).expanduser()
        label = f"lead file {path.name}"
        suffix = path.suffix.lower()
        if suffix not in (".csv", ".json"):
            raise LeadSourceError(f"{label}: unsupported file type {suffix or '(none)'}; use .csv or .json")
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise LeadSourceError(f"{label} is larger than {MAX_SOURCE_BYTES // (1024 * 1024)} MB")
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            raise LeadSourceError(f"lead file not found: {path}") from None
        except UnicodeDecodeError:
            raise LeadSourceError(f"{label} is not UTF-8 text") from None
        except OSError as exc:
            raise LeadSourceError(f"could not read {label}: {exc.strerror or exc}") from None

        if suffix == ".csv":
            return parse_csv_text(text, label)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LeadSourceError(f"{label} is not valid JSON (line {exc.lineno})") from None
        return parse_json_leads(data, label)


_SHEET_ID = re.compile(r"^[A-Za-z0-9_-]{25,}$")


def to_csv_export_url(source_ref: str) -> str:
    """Normalize a Google Sheet ID, sheet URL, published URL or direct CSV URL into a CSV download URL."""
    ref = source_ref.strip()
    if _SHEET_ID.fullmatch(ref):
        return f"https://docs.google.com/spreadsheets/d/{ref}/export?format=csv"

    parts = urllib.parse.urlsplit(ref)
    if parts.scheme != "https" or not parts.netloc:
        raise LeadSourceError("Google Sheet source must be an https:// URL or a sheet ID")

    if parts.netloc.lower() != "docs.google.com" or not parts.path.startswith("/spreadsheets/d/"):
        return ref  # a direct CSV export URL from another host

    segments = parts.path.split("/")  # ['', 'spreadsheets', 'd', <id or 'e'>, ...]
    query = urllib.parse.parse_qs(parts.query)
    fragment = urllib.parse.parse_qs(parts.fragment)
    gid = (query.get("gid") or fragment.get("gid") or [None])[0]

    if len(segments) > 4 and segments[3] == "e":
        # "Publish to web" URL: /spreadsheets/d/e/<published id>/pub
        params = {"output": "csv", **({"gid": gid} if gid else {})}
        return f"https://docs.google.com/spreadsheets/d/e/{segments[4]}/pub?{urllib.parse.urlencode(params)}"

    sheet_id = segments[3] if len(segments) > 3 else ""
    if not _SHEET_ID.fullmatch(sheet_id):
        raise LeadSourceError("Google Sheet URL does not contain a valid sheet ID")
    params = {"format": "csv", **({"gid": gid} if gid else {})}
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?{urllib.parse.urlencode(params)}"


class GoogleSheetCsvLeadSourceAdapter:
    """Downloads a Google Sheet (shared as 'Anyone with the link' or published) as CSV."""

    source_name = "gsheet"
    _SHARING_HINT = "share it as 'Anyone with the link can view' or use File > Share > Publish to web as CSV"

    def __init__(self, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = timeout_seconds

    def fetch_leads(self, source_ref: str) -> list[LeadInput]:
        url = to_csv_export_url(source_ref)
        label = "Google Sheet"
        req = urllib.request.Request(url=url, headers={"Accept": "text/csv"}, method="GET")
        # Never include the URL in errors: sheet IDs and direct CSV links can grant access on their own.
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if resp.status < 200 or resp.status >= 300:
                    raise LeadSourceError(f"{label} request returned status {resp.status}")
                raw = resp.read(MAX_SOURCE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            hint = f"; {self._SHARING_HINT}" if exc.code in (401, 403, 404) else ""
            raise LeadSourceError(f"{label} request failed with status {exc.code}{hint}") from None
        except urllib.error.URLError as exc:
            raise LeadSourceError(f"Failed to reach Google Sheets: {exc.reason}") from None
        except TimeoutError:
            raise LeadSourceError(f"{label} request timed out after {self.timeout_seconds:g} seconds") from None

        if len(raw) > MAX_SOURCE_BYTES:
            raise LeadSourceError(f"{label} is larger than {MAX_SOURCE_BYTES // (1024 * 1024)} MB")
        head = raw.lstrip()[:20].lower()
        if head.startswith((b"<!doctype html", b"<html")):
            raise LeadSourceError(f"{label} returned a web page instead of CSV; {self._SHARING_HINT}")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise LeadSourceError(f"{label} did not return UTF-8 CSV") from None
        return parse_csv_text(text, label)
