from typing import Protocol
from gtm_engine.contracts.lead_source import LeadInput


class LeadSourceError(RuntimeError):
    """A lead source could not be read: missing file, malformed data, or an HTTP/network failure.

    Messages never include full URLs or query strings, which may carry access tokens.
    """


class LeadSourcePort(Protocol):
    source_name: str

    def fetch_leads(self, source_ref: str) -> list[LeadInput]:
        """Load raw leads from a file path, URL or ID. Raise LeadSourceError on failure."""
        ...
