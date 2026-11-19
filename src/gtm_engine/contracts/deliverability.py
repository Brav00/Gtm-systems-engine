from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field


class DomainAuditResult(BaseModel):
    domain: str
    provider: str
    audited_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    mx_present: bool
    spf_present: bool
    spf_valid_syntax: bool
    dkim_selector_found: bool
    dmarc_present: bool
    dmarc_policy: Literal["none", "quarantine", "reject", "missing"] = "missing"
    tracking_cname_valid: bool
    gate_passed: bool
    reasons: list[str] = Field(default_factory=list)


class AuditBatchReport(BaseModel):
    total_domains: int
    passed_count: int
    failed_count: int
    results: list[DomainAuditResult]
