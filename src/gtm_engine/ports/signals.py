from typing import Protocol
from gtm_engine.contracts.signals import SignalBatchReport, SignalTargetBoard


class SignalCollectorPort(Protocol):
    provider_name: str

    def collect_signals(self, targets: list[SignalTargetBoard], keywords: list[str]) -> SignalBatchReport:
        """Scan each target's public data and return matching intent signals.

        A failure on one target must be recorded in report.errors, not abort the whole batch.
        """
        ...
