import dns.resolver
from gtm_engine.contracts.deliverability import DomainAuditResult


class LiveDnsDeliverabilityAdapter:
    def __init__(self, timeout: float = 3.0) -> None:
        self.resolver = dns.resolver.Resolver()
        self.resolver.lifetime = timeout
        self.resolver.timeout = timeout

    def _txt_records(self, qname: str) -> list[str]:
        try:
            answers = self.resolver.resolve(qname, "TXT")
            return ["".join(part.decode("utf-8", errors="ignore") for part in rdata.strings) for rdata in answers]
        except Exception:
            return []

    def _has_records(self, qname: str, rdtype: str) -> bool:
        try:
            answers = self.resolver.resolve(qname, rdtype)
            return len(answers) > 0
        except Exception:
            return False

    def audit_domain(self, domain: str, dkim_selector: str, tracking_host: str) -> DomainAuditResult:
        reasons: list[str] = []

        mx_present = self._has_records(domain, "MX")
        if not mx_present:
            reasons.append("missing_mx")

        root_txt = self._txt_records(domain)
        spf_records = [rec for rec in root_txt if rec.lower().startswith("v=spf1")]
        spf_present = len(spf_records) > 0
        spf_valid_syntax = len(spf_records) == 1 and (
            "~" in spf_records[0] or "-" in spf_records[0] or "include:" in spf_records[0]
        )
        if not spf_present or not spf_valid_syntax:
            reasons.append("invalid_or_missing_spf")

        dkim_qname = f"{dkim_selector}._domainkey.{domain}"
        dkim_txt = self._txt_records(dkim_qname)
        dkim_selector_found = any(
            "v=dkim1" in rec.lower() or "p=" in rec.lower() for rec in dkim_txt
        ) or self._has_records(dkim_qname, "CNAME")
        if not dkim_selector_found:
            reasons.append(f"missing_dkim_selector:{dkim_selector}")

        dmarc_txt = self._txt_records(f"_dmarc.{domain}")
        dmarc_records = [rec for rec in dmarc_txt if "v=dmarc1" in rec.lower()]
        dmarc_present = len(dmarc_records) > 0
        policy: str = "missing"
        if dmarc_present:
            for tag in dmarc_records[0].lower().split(";"):
                tag = tag.strip()
                if tag.startswith("p="):
                    val = tag.split("=", 1)[1].strip()
                    if val in {"none", "quarantine", "reject"}:
                        policy = val
        if not dmarc_present or policy == "missing":
            reasons.append("missing_dmarc")

        fqdn_tracking = tracking_host if tracking_host.endswith(domain) else f"{tracking_host}.{domain}"
        tracking_cname_valid = self._has_records(fqdn_tracking, "CNAME") or self._has_records(fqdn_tracking, "A")
        if not tracking_cname_valid:
            reasons.append(f"invalid_tracking_cname:{fqdn_tracking}")

        return DomainAuditResult(
            domain=domain,
            provider="dns_live",
            mx_present=mx_present,
            spf_present=spf_present,
            spf_valid_syntax=spf_valid_syntax,
            dkim_selector_found=dkim_selector_found,
            dmarc_present=dmarc_present,
            dmarc_policy=policy,  # type: ignore[arg-type]
            tracking_cname_valid=tracking_cname_valid,
            gate_passed=len(reasons) == 0,
            reasons=reasons,
        )
