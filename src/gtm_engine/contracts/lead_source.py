from typing import Any

from pydantic import BaseModel, Field, field_validator


class LeadInput(BaseModel):
    """One raw lead from any source (Clay, Cargo, a Google Sheet, a CSV or JSON file, or mock data)."""

    email: str
    first_name: str | None = None
    last_name: str | None = None
    company_domain: str | None = None
    company_name: str | None = None
    signal_hook: str | None = None
    # Source columns that have no standard field, kept for reference; never uploaded automatically.
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("company_domain", mode="before")
    @classmethod
    def _normalize_domain(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower() or None
        return value

    @field_validator("first_name", "last_name", "company_name", "signal_hook", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            return " ".join(value.split()) or None
        return value
