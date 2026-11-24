from collections.abc import Sequence

from gtm_engine.contracts.leads import (
    LeadVerificationResult,
    VerificationStatus,
    is_valid_email_syntax,
    normalize_email,
)
from gtm_engine.ports.verifier import EmailVerifierPort


# A clear answer from any provider ends the waterfall; anything else escalates to the next provider.
_FINAL_STATUSES: frozenset[VerificationStatus] = frozenset({"valid", "invalid"})


class VerificationWaterfall:
    def __init__(self, providers: Sequence[EmailVerifierPort]) -> None:
        if not providers:
            raise ValueError("VerificationWaterfall needs at least one provider")
        self.providers = list(providers)

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
                # Provider outage or API error: treat as unknown and fall back to the next provider.
                result_status, result_reasons = "unknown", [f"provider_error:{exc}"]

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
