from gtm_engine.contracts.leads import VerificationStatus, is_valid_email_syntax, normalize_email


class MockEmailVerifierAdapter:
    """Deterministic offline verifier: the result is decided by keywords in the email address."""

    def __init__(self, provider_name: str = "mock_verifier") -> None:
        self.provider_name = provider_name

    def verify_email(self, email: str) -> tuple[VerificationStatus, list[str]]:
        address = normalize_email(email)

        if not is_valid_email_syntax(address):
            return "invalid", ["invalid_syntax"]
        if "invalid" in address or "bounce" in address:
            return "invalid", ["mock_mailbox_not_found"]
        if "catchall" in address or "catch_all" in address or "risky" in address:
            return "risky", ["mock_catch_all"]
        if "unknown" in address:
            return "unknown", ["mock_unknown"]
        return "valid", ["mock_mailbox_ok"]
