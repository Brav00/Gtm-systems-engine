from typing import Protocol
from gtm_engine.contracts.leads import VerificationStatus


class VerifierError(RuntimeError):
    """A provider call failed (network error, timeout, bad response). Retrying other leads may work."""


class VerifierAuthError(VerifierError):
    """The provider rejected the account (bad or expired key, forbidden, out of credits). Every call will fail."""


class EmailVerifierPort(Protocol):
    provider_name: str

    def verify_email(self, email: str) -> tuple[VerificationStatus, list[str]]:
        """Verify one normalized email address and return its status plus reason codes.

        Raise VerifierAuthError for account-level failures and VerifierError for other call failures.
        """
        ...
