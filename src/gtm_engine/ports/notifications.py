from typing import Protocol
from gtm_engine.contracts.readiness import ReadinessBatchReport


class ReadinessNotifierPort(Protocol):
    def notify_readiness(self, report: ReadinessBatchReport) -> None:
        ...
