from typing import Protocol
from gtm_engine.contracts.leads import VerificationStatus


class EmailVerifierPort(Protocol):
    provider_name: str

    def verify_email(self, email: str) -> tuple[VerificationStatus, list[str]]:
        """Verify one normalized email address and return its status plus reason codes."""
        ...
