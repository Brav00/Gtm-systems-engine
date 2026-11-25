from typing import Any

from gtm_engine.adapters.smartlead_client import SmartleadApiError, SmartleadClient
from gtm_engine.contracts.campaign import (
    CampaignProvisioningReport,
    CampaignProvisioningRequest,
    ProvisioningGuardrailError,
    select_provisionable,
)
from gtm_engine.contracts.leads import LeadVerificationResult


class CampaignProvisioningError(RuntimeError):
    """A Smartlead write failed part-way. The message says what was already created or uploaded."""


def _email_domain(email: str) -> str:
    return email.strip().lower().rsplit("@", 1)[-1] if "@" in email else ""


def _is_healthy_mailbox(account: dict) -> bool:
    """Same health bar as the warm-up gate: SMTP connected, warm-up ACTIVE and not blocked."""
    details = account.get("warmup_details") or {}
    return (
        account.get("is_smtp_success") is not False
        and details.get("is_warmup_blocked") is not True
        and details.get("status") == "ACTIVE"
    )


def _lead_payload(lead: LeadVerificationResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "email": lead.email,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company_name": lead.company_name,
        "website": lead.company_domain,
    }
    if lead.signal_hook:
        # Available in Smartlead sequences as {{signal_hook}}.
        payload["custom_fields"] = {"signal_hook": lead.signal_hook}
    return {k: v for k, v in payload.items() if v}


class LiveSmartleadCampaignAdapter:
    provider_name = "smartlead"
    LEAD_BATCH_SIZE = 100

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://server.smartlead.ai/api/v1",
        timeout_seconds: float = 15.0,
    ) -> None:
        self.client = SmartleadClient(api_key, base_url=base_url, timeout_seconds=timeout_seconds)

    def _select_accounts(self, domains: list[str]) -> tuple[list[int], list[str], list[str], int]:
        allowed = set(domains)
        ids_by_domain: dict[str, list[int]] = {d: [] for d in domains}
        unhealthy = 0
        for account in self.client.list_email_accounts():
            domain = _email_domain(str(account.get("from_email", "")))
            if domain not in allowed:
                continue  # never attach mailboxes on quarantined/blocked or unknown domains
            if not _is_healthy_mailbox(account):
                unhealthy += 1
                continue
            try:
                ids_by_domain[domain].append(int(account["id"]))
            except (KeyError, TypeError, ValueError):
                unhealthy += 1

        account_ids = [i for d in domains for i in ids_by_domain[d]]
        attached = [d for d in domains if ids_by_domain[d]]
        no_mailboxes = [d for d in domains if not ids_by_domain[d]]
        return account_ids, attached, no_mailboxes, unhealthy

    def provision_campaign(self, request: CampaignProvisioningRequest) -> CampaignProvisioningReport:
        # 1. Guardrails, then read-only lookups. No write call happens until all of these pass.
        selection = select_provisionable(request)
        reasons = list(selection.reasons)

        account_ids, attached, no_mailboxes, unhealthy = self._select_accounts(selection.domains)
        reasons.extend(f"no_healthy_mailboxes:{d}" for d in no_mailboxes)
        if unhealthy:
            reasons.append(f"skipped_unhealthy_mailboxes:{unhealthy}")
        if not account_ids:
            raise ProvisioningGuardrailError("no_healthy_mailboxes_on_ready_domains")

        if request.campaign_id is not None:
            existing: Any = self.client.request("GET", f"/campaigns/{request.campaign_id}")
            status = str((existing or {}).get("status") or "UNKNOWN").upper()
            if status == "ACTIVE" and not request.activate_campaign:
                # Adding leads to a running campaign starts sending; require an explicit --activate.
                raise ProvisioningGuardrailError("campaign_already_active")

        # 2. Writes.
        if request.campaign_id is None:
            created: Any = self.client.request("POST", "/campaigns/create", {"name": request.campaign_name})
            raw_id = (created or {}).get("id", (created or {}).get("campaign_id"))
            try:
                campaign_id = int(raw_id)
            except (TypeError, ValueError):
                raise CampaignProvisioningError("Smartlead /campaigns/create response had no campaign id") from None
            status = "DRAFTED"
            reasons.append("campaign_created")
        else:
            campaign_id = request.campaign_id
            reasons.append("campaign_reused")

        uploaded = 0
        step = "attaching email accounts"
        try:
            self.client.request(
                "POST", f"/campaigns/{campaign_id}/email-accounts", {"email_account_ids": account_ids}
            )

            step = "uploading leads"
            for start in range(0, len(selection.leads), self.LEAD_BATCH_SIZE):
                batch = selection.leads[start : start + self.LEAD_BATCH_SIZE]
                response: Any = self.client.request(
                    "POST",
                    f"/campaigns/{campaign_id}/leads",
                    {
                        "lead_list": [_lead_payload(lead) for lead in batch],
                        "settings": {
                            "ignore_global_block_list": False,
                            "ignore_unsubscribe_list": False,
                            "ignore_community_bounce_list": False,
                        },
                    },
                )
                count = (response or {}).get("upload_count") if isinstance(response, dict) else None
                uploaded += count if isinstance(count, int) and 0 <= count <= len(batch) else len(batch)

            if request.activate_campaign:
                step = "starting the campaign"
                self.client.request("POST", f"/campaigns/{campaign_id}/status", {"status": "START"})
                status = "ACTIVE"
                reasons.append("campaign_activated")
            else:
                reasons.append(f"campaign_left_{status.lower()}:pass --activate to start sending")
        except SmartleadApiError as exc:
            raise CampaignProvisioningError(
                f"{exc} while {step} [campaign {campaign_id} status {status}; "
                f"{uploaded} of {len(selection.leads)} leads uploaded before the failure]"
            ) from None

        not_added = len(selection.leads) - uploaded
        if not_added:
            reasons.append(f"smartlead_did_not_add_leads:{not_added}")

        return CampaignProvisioningReport(
            provider=self.provider_name,
            campaign_id=campaign_id,
            campaign_name=request.campaign_name,
            attached_domains=attached,
            excluded_domains=list(request.excluded_domains) + no_mailboxes,
            attached_account_ids=account_ids,
            uploaded_leads_count=uploaded,
            skipped_leads_count=selection.skipped_leads_count,
            total_daily_capacity=sum(request.domain_daily_caps.get(d, 0) for d in attached),
            campaign_status=status,
            reasons=reasons,
        )
