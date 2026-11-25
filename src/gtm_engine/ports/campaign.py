from typing import Protocol
from gtm_engine.contracts.campaign import CampaignProvisioningReport, CampaignProvisioningRequest


class CampaignProvisionerPort(Protocol):
    def provision_campaign(self, request: CampaignProvisioningRequest) -> CampaignProvisioningReport:
        """Create or reuse a campaign, attach healthy inboxes on ready domains, and upload valid leads.

        Must call select_provisionable(request) before any write, so guardrails block unsafe runs.
        """
        ...
