from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field


RotationStatus = Literal["active", "throttled", "quarantined"]


class DomainWarmupEvaluation(BaseModel):
    domain: str
    provider: str
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    warmup_score: int = Field(ge=0, le=100)
    bounce_rate_pct: float = Field(ge=0.0, le=100.0)
    daily_send_limit: int = Field(ge=0)
    recommended_daily_cap: int = Field(ge=0)
    rotation_status: RotationStatus
    gate_passed: bool
    reasons: list[str] = Field(default_factory=list)


class WarmupBatchReport(BaseModel):
    total_domains: int
    active_count: int
    throttled_count: int
    quarantined_count: int
    results: list[DomainWarmupEvaluation]
