import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


VerificationStatus = Literal["valid", "risky", "invalid", "unknown"]

# Basic shape check only (something@domain.tld); providers do the real mailbox verification.
_EMAIL_SYNTAX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def is_valid_email_syntax(email: str) -> bool:
    return bool(_EMAIL_SYNTAX.fullmatch(email))


class LeadInput(BaseModel):
    email: str
    first_name: str | None = None
    last_name: str | None = None
    company_domain: str | None = None

    @field_validator("email", "company_domain", mode="before")
    @classmethod
    def _normalize(cls, value: object) -> object:
        return normalize_email(value) if isinstance(value, str) else value


class LeadVerificationResult(BaseModel):
    email: str
    first_name: str | None = None
    last_name: str | None = None
    company_domain: str | None = None
    status: VerificationStatus
    deliverable: bool = False
    provider: str
    attempts: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _deliverable_only_when_valid(self) -> "LeadVerificationResult":
        self.deliverable = self.status == "valid"
        return self


class VerificationBatchReport(BaseModel):
    total_leads: int
    valid_count: int
    risky_count: int
    invalid_count: int
    unknown_count: int
    deliverable_emails: list[str]
    deliverable_leads: list[LeadVerificationResult]
    results: list[LeadVerificationResult]
