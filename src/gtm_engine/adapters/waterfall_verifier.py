from collections.abc import Sequence

from gtm_engine.contracts.leads import (
    LeadInput,
    LeadVerificationResult,
    VerificationStatus,
    is_valid_email_syntax,
    normalize_email,
)
from gtm_engine.ports.verifier import EmailVerifierPort, VerifierAuthError


# A clear answer from any provider ends the waterfall; anything else escalates to the next provider.
_FINAL_STATUSES: frozenset[VerificationStatus] = frozenset({"valid", "invalid"})


class VerificationAbortedError(RuntimeError):
    """Raised when the circuit breaker trips: a provider auth failure or too many errors in a row."""


class VerificationWaterfall:
    def __init__(self, providers: Sequence[EmailVerifierPort], max_consecutive_errors: int = 3) -> None:
        if not providers:
            raise ValueError("VerificationWaterfall needs at least one provider")
        if max_consecutive_errors < 1:
            raise ValueError("max_consecutive_errors must be at least 1")
        self.providers = list(providers)
        self.max_consecutive_errors = max_consecutive_errors
        # Circuit breaker state: errors in a row per provider, reset by any successful call.
        self._consecutive_errors: dict[str, int] = {}

    def _record_error(self, provider_name: str, exc: RuntimeError) -> None:
        if isinstance(exc, VerifierAuthError):
            raise VerificationAbortedError(
                f"Aborting verification: {provider_name} authentication/account error: {exc}"
            ) from None

        count = self._consecutive_errors.get(provider_name, 0) + 1
        self._consecutive_errors[provider_name] = count
        if count >= self.max_consecutive_errors:
            raise VerificationAbortedError(
                f"Aborting verification: {provider_name} failed {count} times in a row (last error: {exc})"
            ) from None

    def verify(self, email: str) -> LeadVerificationResult:
        address = normalize_email(email)

        # Free local check first: malformed addresses never spend provider credits.
        if not is_valid_email_syntax(address):
            return LeadVerificationResult(
                email=address,
                status="invalid",
                provider="syntax_check",
                attempts=0,
                reasons=["invalid_syntax"],
            )

        status: VerificationStatus = "unknown"
        provider = self.providers[0].provider_name
        reasons: list[str] = []
        attempts = 0

        for verifier in self.providers:
            attempts += 1
            try:
                result_status, result_reasons = verifier.verify_email(address)
            except RuntimeError as exc:
                # Trips the circuit breaker on auth failure or repeated errors; otherwise
                # treat this call as unknown and fall back to the next provider.
                self._record_error(verifier.provider_name, exc)
                result_status, result_reasons = "unknown", [f"provider_error:{exc}"]
            else:
                self._consecutive_errors[verifier.provider_name] = 0

            reasons.extend(f"{verifier.provider_name}:{reason}" for reason in result_reasons)

            # A later "unknown" never overrides an earlier, more informative "risky".
            if not (result_status == "unknown" and status == "risky"):
                status = result_status
                provider = verifier.provider_name

            if status in _FINAL_STATUSES:
                break

        return LeadVerificationResult(
            email=address,
            status=status,
            provider=provider,
            attempts=attempts,
            reasons=reasons,
        )

    def verify_lead(self, lead: LeadInput) -> LeadVerificationResult:
        return with_lead_context(self.verify(lead.email), lead)


def with_lead_context(result: LeadVerificationResult, lead: LeadInput) -> LeadVerificationResult:
    """Copy the lead's name and company onto a verification result, ready for CRM / Smartlead upload."""
    return result.model_copy(
        update={
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "company_domain": lead.company_domain,
            "company_name": lead.company_name,
            "signal_hook": lead.signal_hook,
        }
    )
