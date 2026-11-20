from datetime import datetime, timezone
from pydantic import BaseModel, Field
from gtm_engine.contracts.deliverability import DomainAuditResult
from gtm_engine.contracts.warmup import DomainWarmupEvaluation, RotationStatus


class DomainReadinessDecision(BaseModel):
    domain: str
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ready_to_send: bool
    rotation_status: RotationStatus
    effective_daily_cap: int = Field(ge=0)
    blocking_reasons: list[str] = Field(default_factory=list)
    dns_report: DomainAuditResult
    warmup_report: DomainWarmupEvaluation


class ReadinessBatchReport(BaseModel):
    total_domains: int
    ready_count: int
    blocked_count: int
    total_daily_capacity: int = Field(ge=0)
    decisions: list[DomainReadinessDecision]
