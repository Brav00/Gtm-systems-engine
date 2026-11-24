from typing import Protocol
from gtm_engine.contracts.leads import VerificationBatchReport
from gtm_engine.contracts.readiness import ReadinessBatchReport


class ReadinessNotifierPort(Protocol):
    def notify_readiness(self, report: ReadinessBatchReport) -> None:
        ...


class VerificationNotifierPort(Protocol):
    def notify_verification(self, report: VerificationBatchReport) -> None:
        ...
