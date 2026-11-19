from typing import Protocol
from gtm_engine.contracts.deliverability import DomainAuditResult


class DeliverabilityAuditorPort(Protocol):
    def audit_domain(self, domain: str, dkim_selector: str, tracking_host: str) -> DomainAuditResult:
        """Audit DNS authentication records for a sending domain and return a normalized contract."""
        ...
