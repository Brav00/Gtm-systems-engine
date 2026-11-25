from gtm_engine.contracts.campaign import CampaignProvisioningReport
from gtm_engine.contracts.leads import VerificationBatchReport
from gtm_engine.contracts.readiness import ReadinessBatchReport
from gtm_engine.contracts.signals import SignalBatchReport


class NoopReadinessNotifier:
    def notify_readiness(self, report: ReadinessBatchReport) -> None:
        return None

    def notify_verification(self, report: VerificationBatchReport) -> None:
        return None

    def notify_campaign(self, report: CampaignProvisioningReport) -> None:
        return None

    def notify_signals(self, report: SignalBatchReport) -> None:
        return None
