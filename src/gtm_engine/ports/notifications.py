from typing import Protocol
from gtm_engine.contracts.campaign import CampaignProvisioningReport
from gtm_engine.contracts.leads import VerificationBatchReport
from gtm_engine.contracts.readiness import ReadinessBatchReport
from gtm_engine.contracts.signals import SignalBatchReport


class ReadinessNotifierPort(Protocol):
    def notify_readiness(self, report: ReadinessBatchReport) -> None:
        ...


class VerificationNotifierPort(Protocol):
    def notify_verification(self, report: VerificationBatchReport) -> None:
        ...


class CampaignNotifierPort(Protocol):
    def notify_campaign(self, report: CampaignProvisioningReport) -> None:
        ...


class SignalNotifierPort(Protocol):
    def notify_signals(self, report: SignalBatchReport) -> None:
        ...
