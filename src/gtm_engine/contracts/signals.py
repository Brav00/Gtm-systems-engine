import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, field_validator


SignalType = Literal["hiring_intent", "tech_stack_change", "funding_or_expansion", "regulatory_trigger"]

# Board tokens go into a URL path, so only allow the characters job boards actually use.
_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class SignalTargetBoard(BaseModel):
    company_name: str
    company_domain: str
    board_token: str

    @field_validator("company_name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("company_name must be a non-empty string")
        return value.strip()

    @field_validator("company_domain")
    @classmethod
    def _normalize_domain(cls, value: str) -> str:
        domain = value.strip().lower()
        if not domain or "." not in domain or "/" in domain:
            raise ValueError("company_domain must look like example.com")
        return domain

    @field_validator("board_token")
    @classmethod
    def _valid_token(cls, value: str) -> str:
        token = value.strip()
        if not _BOARD_TOKEN.fullmatch(token):
            raise ValueError("board_token may only contain letters, digits, '-' and '_'")
        return token


class IntentSignal(BaseModel):
    signal_id: str
    company_name: str
    company_domain: str
    signal_type: SignalType
    title: str
    location: str | None = None
    evidence_url: str
    matched_keywords: list[str] = Field(default_factory=list)
    signal_hook: str
    detected_at: str


class SignalBatchReport(BaseModel):
    provider: str
    total_scanned: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    unique_companies: list[str]
    signals: list[IntentSignal]
    errors: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class JobPosting:
    """A raw open role from any job board, before keyword filtering."""

    target: SignalTargetBoard
    title: str
    location: str | None
    evidence_url: str


def normalize_keywords(keywords: list[str]) -> list[str]:
    """Trim, drop blanks and de-duplicate case-insensitively, keeping the first spelling."""
    seen: dict[str, str] = {}
    for keyword in keywords:
        cleaned = keyword.strip()
        if cleaned:
            seen.setdefault(cleaned.lower(), cleaned)
    return list(seen.values())


def match_keywords(title: str, keywords: list[str]) -> list[str]:
    """Case-insensitive substring match. Empty keywords match every role."""
    lowered = title.lower()
    return [k for k in keywords if k.lower() in lowered]


def make_signal_id(company_domain: str, signal_type: SignalType, title: str) -> str:
    """Deterministic ID: the same company, signal type and title always give the same ID."""
    normalized_title = " ".join(title.lower().split())
    digest = hashlib.sha256(f"{company_domain.lower()}|{signal_type}|{normalized_title}".encode("utf-8"))
    return f"sig_{digest.hexdigest()[:16]}"


# Checked in order; the first theme whose terms appear in the title or a matched keyword wins.
_HOOK_THEMES: list[tuple[tuple[str, ...], str]] = [
    (("revops", "revenue operations", "sales operations", "sales ops", "gtm operations"),
     "investment in revenue operations"),
    (("chief revenue", "cro", "vp sales", "head of sales"), "new GTM leadership"),
    (("account executive", "sdr", "bdr", "sales development", "business development", "sales"),
     "active sales team expansion"),
    (("growth", "demand gen", "demand generation", "marketing"), "active growth and marketing investment"),
    (("revenue",), "active GTM scaling"),
]


def build_signal_hook(title: str, matched_keywords: list[str]) -> str:
    haystack = " ".join([title.lower(), *(k.lower() for k in matched_keywords)])
    words = set(re.findall(r"[a-z]+", haystack))
    theme = "active GTM scaling"
    for terms, candidate in _HOOK_THEMES:
        if any((term in words) if " " not in term else (term in haystack) for term in terms):
            theme = candidate
            break
    return f"Hiring for {title} — indicates {theme}"


def build_signal_report(
    provider: str,
    postings: list[JobPosting],
    keywords: list[str],
    detected_at: str,
    errors: list[str] | None = None,
) -> SignalBatchReport:
    """Shared pipeline for every collector: keyword filter → deduplicate by signal_id → hook."""
    wanted = normalize_keywords(keywords)
    signals: list[IntentSignal] = []
    seen_ids: set[str] = set()

    for posting in postings:
        title = " ".join(posting.title.split())
        matched = match_keywords(title, wanted)
        if wanted and not matched:
            continue
        signal_id = make_signal_id(posting.target.company_domain, "hiring_intent", title)
        if signal_id in seen_ids:
            continue
        seen_ids.add(signal_id)
        signals.append(
            IntentSignal(
                signal_id=signal_id,
                company_name=posting.target.company_name,
                company_domain=posting.target.company_domain,
                signal_type="hiring_intent",
                title=title,
                location=posting.location,
                evidence_url=posting.evidence_url,
                matched_keywords=matched,
                signal_hook=build_signal_hook(title, matched),
                detected_at=detected_at,
            )
        )

    return SignalBatchReport(
        provider=provider,
        total_scanned=len(postings),
        matched_count=len(signals),
        unique_companies=list(dict.fromkeys(s.company_name for s in signals)),
        signals=signals,
        errors=list(errors or []),
    )
