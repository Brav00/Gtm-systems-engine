from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

from gtm_engine.contracts.leads import LeadVerificationResult


class ProvisioningGuardrailError(ValueError):
    """A safety rule blocked provisioning before any write call. `code` is a stable reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _normalize_domains(value: object) -> object:
    if not isinstance(value, list):
        return value
    seen: dict[str, None] = {}
    for item in value:
        if isinstance(item, str) and item.strip():
            seen.setdefault(item.strip().lower(), None)
    return list(seen)


class CampaignProvisioningRequest(BaseModel):
    campaign_name: str
    # Existing campaign to add to; None creates a new campaign.
    campaign_id: int | None = Field(default=None, ge=1)
    # Only Day 1 domains with ready_to_send=True and a daily cap > 0.
    allowed_domains: list[str]
    # Day 1 domains that were not ready, reported back for visibility.
    excluded_domains: list[str] = Field(default_factory=list)
    # Day 1 effective daily cap per allowed domain, used for total_daily_capacity.
    domain_daily_caps: dict[str, int] = Field(default_factory=dict)
    # Day 2 results; anything not deliverable/valid is stripped by the guardrails before upload.
    leads: list[LeadVerificationResult]
    # Campaigns stay drafted/paused unless this is explicitly True.
    activate_campaign: bool = False

    @field_validator("campaign_name")
    @classmethod
    def _campaign_name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("campaign_name must be a non-empty string")
        return value.strip()

    @field_validator("allowed_domains", "excluded_domains", mode="before")
    @classmethod
    def _normalize_domain_lists(cls, value: object) -> object:
        return _normalize_domains(value)

    @field_validator("domain_daily_caps", mode="before")
    @classmethod
    def _normalize_cap_keys(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {str(k).strip().lower(): v for k, v in value.items()}


class CampaignProvisioningReport(BaseModel):
    provider: str
    campaign_id: int
    campaign_name: str
    attached_domains: list[str]
    excluded_domains: list[str]
    attached_account_ids: list[int]
    uploaded_leads_count: int = Field(ge=0)
    skipped_leads_count: int = Field(ge=0)
    total_daily_capacity: int = Field(ge=0)
    campaign_status: str
    reasons: list[str] = Field(default_factory=list)


@dataclass
class ProvisioningSelection:
    domains: list[str]
    leads: list[LeadVerificationResult]
    skipped_leads_count: int
    reasons: list[str] = field(default_factory=list)


def select_provisionable(request: CampaignProvisioningRequest) -> ProvisioningSelection:
    """Hard guardrails shared by every provisioner. Must run before any write API call."""
    if not request.allowed_domains:
        raise ProvisioningGuardrailError("no_ready_domains_available")

    valid: list[LeadVerificationResult] = []
    seen: set[str] = set()
    non_valid = 0
    duplicates = 0
    for lead in request.leads:
        if not (lead.deliverable and lead.status == "valid"):
            non_valid += 1
        elif lead.email in seen:
            duplicates += 1
        else:
            seen.add(lead.email)
            valid.append(lead)

    if not valid:
        raise ProvisioningGuardrailError("no_valid_leads_to_upload")

    reasons = [f"excluded_domain_not_ready:{d}" for d in request.excluded_domains]
    if non_valid:
        reasons.append(f"skipped_non_valid_leads:{non_valid}")
    if duplicates:
        reasons.append(f"skipped_duplicate_leads:{duplicates}")

    return ProvisioningSelection(
        domains=list(request.allowed_domains),
        leads=valid,
        skipped_leads_count=non_valid + duplicates,
        reasons=reasons,
    )
