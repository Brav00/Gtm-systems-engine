from typing import Protocol
from gtm_engine.contracts.campaign import CampaignProvisioningReport
from gtm_engine.contracts.leads import VerificationBatchReport
from gtm_engine.contracts.pipeline import PipelineRunSummary
from gtm_engine.contracts.readiness import ReadinessBatchReport


class ReadinessNotifierPort(Protocol):
    def notify_readiness(self, report: ReadinessBatchReport) -> None:
        ...


class VerificationNotifierPort(Protocol):
    def notify_verification(self, report: VerificationBatchReport) -> None:
        ...


class CampaignNotifierPort(Protocol):
    def notify_campaign(self, report: CampaignProvisioningReport) -> None:
        ...


class PipelineNotifierPort(Protocol):
    def notify_pipeline(self, summary: PipelineRunSummary) -> None:
        ...
