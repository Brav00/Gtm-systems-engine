from gtm_engine.contracts.campaign import CampaignProvisioningReport
from gtm_engine.contracts.leads import VerificationBatchReport
from gtm_engine.contracts.readiness import ReadinessBatchReport


class NoopReadinessNotifier:
    def notify_readiness(self, report: ReadinessBatchReport) -> None:
        return None

    def notify_verification(self, report: VerificationBatchReport) -> None:
        return None

    def notify_campaign(self, report: CampaignProvisioningReport) -> None:
        return None
