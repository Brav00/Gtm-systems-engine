from typing import Literal

from pydantic import BaseModel, Field


PipelineStatus = Literal["provisioned", "blocked", "error"]


class PipelineRunSummary(BaseModel):
    """One end-to-end run: lead source -> verification -> readiness -> campaign provisioning."""

    run_id: str
    status: PipelineStatus
    exit_code: int
    mode: Literal["mock", "live"]
    source_type: Literal["mock", "file", "gsheet"]
    campaign_name: str
    campaign_id: int | None = None
    launch_requested: bool
    campaign_status: str | None = None
    leads_loaded: int = 0
    valid_leads: int = 0
    risky_leads: int = 0
    invalid_leads: int = 0
    unknown_leads: int = 0
    domains_checked: list[str] = Field(default_factory=list)
    ready_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)
    uploaded_leads: int = 0
    total_daily_capacity: int = 0
    reasons: list[str] = Field(default_factory=list)
    output_dir: str
    artifacts: dict[str, str] = Field(default_factory=dict)
    started_at: str
    finished_at: str | None = None
