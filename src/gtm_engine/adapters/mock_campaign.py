from gtm_engine.contracts.campaign import (
    CampaignProvisioningReport,
    CampaignProvisioningRequest,
    select_provisionable,
)


class MockCampaignProvisionerAdapter:
    """Offline provisioner: simulates campaign 9001 with one mock mailbox per ready domain."""

    provider_name = "mock"
    MOCK_CAMPAIGN_ID = 9001
    MOCK_FIRST_ACCOUNT_ID = 1001
    LEAD_BATCH_SIZE = 100

    def __init__(self) -> None:
        # Simulated write calls, in order, so tests can prove guardrails block before any write.
        self.write_calls: list[str] = []

    def provision_campaign(self, request: CampaignProvisioningRequest) -> CampaignProvisioningReport:
        selection = select_provisionable(request)
        reasons = list(selection.reasons)

        if request.campaign_id is None:
            campaign_id = self.MOCK_CAMPAIGN_ID
            self.write_calls.append("create_campaign")
            reasons.append("campaign_created")
        else:
            campaign_id = request.campaign_id
            reasons.append("campaign_reused")

        account_ids = [self.MOCK_FIRST_ACCOUNT_ID + i for i in range(len(selection.domains))]
        self.write_calls.append("attach_email_accounts")

        for _ in range(0, len(selection.leads), self.LEAD_BATCH_SIZE):
            self.write_calls.append("upload_leads")

        if request.activate_campaign:
            self.write_calls.append("start_campaign")
            status = "ACTIVE"
            reasons.append("campaign_activated")
        else:
            status = "DRAFTED"
            reasons.append("campaign_left_drafted:pass --activate to start sending")

        return CampaignProvisioningReport(
            provider=self.provider_name,
            campaign_id=campaign_id,
            campaign_name=request.campaign_name,
            attached_domains=selection.domains,
            excluded_domains=list(request.excluded_domains),
            attached_account_ids=account_ids,
            uploaded_leads_count=len(selection.leads),
            skipped_leads_count=selection.skipped_leads_count,
            total_daily_capacity=sum(request.domain_daily_caps.get(d, 0) for d in selection.domains),
            campaign_status=status,
            reasons=reasons,
        )
