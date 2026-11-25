# GTM Systems Engine

Pre-flight readiness gate for cold-email sending domains. For every domain it checks **DNS authentication** and **mailbox warm-up health**, then decides whether the domain may send today and at what daily cap.

```mermaid
flowchart LR
    subgraph IN["Inputs"]
        D["Domain list<br/>--domains a.com,b.com"]
        F["CLI flags<br/>--dns-mode mock|live<br/>--warmup-mode mock|live<br/>--strict · --only-blocked"]
        S["GitHub Secrets<br/>SMARTLEAD_API_KEY<br/>READINESS_WEBHOOK_URL"]
    end

    subgraph ADAPT["Adapters (Ports & Adapters)"]
        direction TB
        DNS["DNS Check · DeliverabilityAuditorPort<br/>MockDeliverabilityAdapter / LiveDnsDeliverabilityAdapter<br/>MX · SPF · DKIM · DMARC · Tracking CNAME"]
        WU["Warm-Up Monitor · WarmupMonitorPort<br/>MockWarmupMonitorAdapter / SmartleadWarmupAdapter<br/>paginated /email-accounts, cached per batch<br/>worst-case reputation &amp; spam rate"]
    end

    subgraph GATE["Combined Readiness Gate"]
        direction TB
        G{"DNS gate passed<br/>AND warm-up gate passed?"}
        A["🟢 Active<br/>cap up to 30/day"]
        T["🟡 Throttled<br/>cap up to 15/day"]
        Q["🔴 Quarantined / Blocked<br/>cap 0/day"]
        G -->|"yes, healthy"| A
        G -->|"yes, warming"| T
        G -->|"no"| Q
    end

    subgraph OUT["Outputs"]
        J["Structured JSON<br/>ReadinessBatchReport"]
        W["Outbound webhook<br/>Slack / n8n / Clay"]
        E["Exit codes<br/>0 ok · 1 strict block · 2 error"]
    end

    D --> DNS
    D --> WU
    F --> ADAPT
    S -->|"API key"| WU
    DNS --> G
    WU --> G
    A --> J
    T --> J
    Q --> J
    J --> W
    J --> E
    S -.->|"webhook URL"| W
```

## How it decides

| Area | Rule | Result |
|---|---|---|
| **DNS: MX** | At least one MX record | Missing → `missing_mx` |
| **DNS: SPF** | Exactly one `v=spf1` record with a qualifier or `include:` | Missing, duplicated or invalid → `invalid_or_missing_spf` |
| **DNS: DKIM** | `<selector>._domainkey.<domain>` has a key (TXT) or CNAME | Missing → `missing_dkim_selector:<selector>` |
| **DNS: DMARC** | `_dmarc.<domain>` has a policy (`none`, `quarantine` or `reject`) | Missing → `missing_dmarc` |
| **DNS: Tracking** | Tracking host resolves (CNAME or A) | Missing → `invalid_tracking_cname:<host>` |
| **Warm-up: quarantine** | No accounts, SMTP disconnected, warm-up blocked, status ≠ `ACTIVE`, lowest reputation < 70 (Smartlead) / < 65 (mock), or worst spam/bounce rate > 3% | 🔴 cap **0**, gate fails |
| **Warm-up: throttle** | Lowest reputation < 85 or worst spam/bounce rate > 1.5% | 🟡 cap **min(daily limit, 15)**, gate passes |
| **Warm-up: active** | Everything healthy | 🟢 cap **min(daily limit, 30)**, gate passes |
| **Readiness** | Ready only if **both** gates pass; a blocked domain's cap is 0 | Reasons prefixed `dns:` / `warmup:` |

A domain's warm-up health is judged by its **weakest mailbox**: the lowest reputation and the highest spam rate across all its Smartlead accounts. Its daily limit is the sum of their `max_email_per_day`.

## Run it

| Where | How |
|---|---|
| **Install** | `pip install -e ".[dev]"` (Python 3.11+) |
| **Tests** | `pytest -v` |
| **Local, sample data** | `python -m gtm_engine.cli.check_readiness --domains healthy-outreach.com,throttled-sender.com,quarantined-sender.com` |
| **Local, live data** | `SMARTLEAD_API_KEY=… python -m gtm_engine.cli.check_readiness --domains yourdomain.com --dns-mode live --warmup-mode live --selector <dkim-selector> --tracking-host <subdomain> --strict` |
| **Send to a webhook** | Add `--webhook-url <url>` (or set `READINESS_WEBHOOK_URL`); add `--only-blocked` to post only when something is blocked |
| **GitHub Actions: CI** | Every push to `main` / `feat/**` and every PR runs `pytest` |
| **GitHub Actions: daily** | Runs the readiness check at 20:00 UTC (mock data by default) |
| **GitHub Actions: on demand** | Actions → *GTM Readiness CI & Runner* → *Run workflow*, then choose domains, `dns_mode`, `warmup_mode`, selector and tracking host |
| **Secrets** | Settings → Secrets and variables → Actions: `SMARTLEAD_API_KEY`, `READINESS_WEBHOOK_URL` |

**Exit codes:** `0` the run completed · `1` `--strict` and at least one domain is blocked · `2` an error (Smartlead API or webhook failure; the key is never printed).

## Day 2: Cost-optimized lead verification waterfall

Verifies lead emails before they enter a sequence. Free checks run first, clear answers stop at the first provider, and only unsure results pay for a second opinion.

```mermaid
flowchart TD
    subgraph INPUT["Input"]
        RAW["Raw lead list · JSON<br/>email · first_name · last_name · company_domain"]
        MODE["Mode<br/>--mode mock or live"]
    end

    subgraph PRE["Free pre-filter gate · no credits spent"]
        NORM["Normalize email<br/>trim + lowercase"]
        DEDUP{"Already checked<br/>in this batch?"}
        SYN{"Syntax check<br/>name@domain.tld"}
    end

    subgraph WF["Waterfall routing"]
        P1["Provider 1 · primary<br/>MillionVerifier or mock"]
        R1{"Provider 1 result"}
        P2["Provider 2 · fallback verifier<br/>final verification pass"]
        CB{{"Circuit breaker<br/>401/403 auth error: abort now<br/>3 failures in a row: abort<br/>otherwise: count as unknown"}}
    end

    subgraph OUT["Outputs"]
        SPLIT{"Split by final status"}
        V["🟢 Valid<br/>deliverable_leads list"]
        R["🟡 Risky or unknown<br/>catch-all review"]
        X["🔴 Invalid<br/>dropped"]
        REP["VerificationBatchReport JSON"]
        HOOK["Outbound webhook<br/>Clay · n8n · Slack"]
        EXIT["Exit codes<br/>0 ok · 1 strict fail · 2 error"]
    end

    ABORT["⛔ Run aborted<br/>no report · no webhook · exit 2"]

    RAW --> NORM
    MODE --> P1
    NORM --> DEDUP
    DEDUP -->|"yes: reuse cached result"| SPLIT
    DEDUP -->|"no"| SYN
    SYN -->|"malformed: invalid, 0 credits"| SPLIT
    SYN -->|"looks like an email"| P1
    P1 --> R1
    R1 -->|"valid or invalid: stop, save credits"| SPLIT
    R1 -->|"risky catch-all or unknown: escalate"| P2
    P2 --> SPLIT
    P1 -.->|"API error"| CB
    P2 -.->|"API error"| CB
    CB -->|"tripped"| ABORT
    SPLIT --> V
    SPLIT --> R
    SPLIT --> X
    V --> REP
    R --> REP
    X --> REP
    REP --> HOOK
    REP --> EXIT
```

| Area | Rule |
|---|---|
| **Syntax check** | Not shaped like `name@domain.tld` → `invalid` with `attempts: 0`; no provider is called |
| **Stop rule** | `valid` or `invalid` from any provider ends the waterfall |
| **Fallback rule** | `risky` or `unknown` escalates to the next provider; a provider error counts as `unknown` |
| **Tie-break** | A later `unknown` never overrides an earlier `risky` |
| **MillionVerifier mapping** | `ok` → valid · `catch_all`, `unknown` → risky · `invalid`, `disposable` → invalid · anything else → unknown |
| **Deliverable** | `true` only when the final status is `valid` |
| **Duplicates** | The same email appears once per lead in `results`, but is verified only once per batch |
| **Strict mode** | `--strict` exits `1` if any lead is risky, invalid or unknown |
| **Circuit breaker: auth** | A `401` / `403`, or a MillionVerifier account error (bad key, no credits), aborts the whole run immediately with exit `2` |
| **Circuit breaker: outage** | 3 provider errors in a row (network, timeout, 5xx) abort the run with exit `2`; any successful call resets the count |
| **Lead context** | Each result keeps the lead's `first_name`, `last_name` and `company_domain`; `deliverable_leads` is an upload-ready list of valid leads |

| Run | Command |
|---|---|
| **Sample data** | `python -m gtm_engine.cli.verify_leads --leads-file fixtures/sample_leads.json` |
| **Live (MillionVerifier)** | `VERIFIER_API_KEY=… python -m gtm_engine.cli.verify_leads --leads-file leads.json --mode live --strict` |
| **Send to a webhook** | Add `--webhook-url <url>` (or set `VERIFICATION_WEBHOOK_URL`) |

Leads file format: a JSON list of objects with `email` (required) and optional `first_name`, `last_name`, `company_domain`.

## Day 3: Programmatic campaign provisioning

Turns the Day 1 and Day 2 reports into a Smartlead campaign: only healthy inboxes on ready domains are attached, only valid leads are uploaded, and the campaign stays drafted unless you pass `--activate`.

```mermaid
flowchart TD
    subgraph INPUT["Inputs"]
        D1["Day 1 readiness report<br/>ready_to_send + daily cap per domain"]
        D2["Day 2 verification report<br/>status per lead"]
    end

    subgraph GUARD["Safety guardrails · checked before any write"]
        G1{"Ready domains<br/>with cap above 0?"}
        G2{"Valid leads<br/>after stripping risky, invalid, unknown, duplicates?"}
        G3{"Healthy inboxes<br/>on ready domains?"}
    end

    subgraph SL["Smartlead campaign provisioner"]
        C1["Create campaign<br/>or reuse --campaign-id"]
        C2["Attach healthy inboxes<br/>SMTP ok · warm-up ACTIVE · not blocked"]
        C3["Upload valid leads<br/>batches of 100 · block lists respected"]
        C4{"--activate?"}
        DRAFT["Stay drafted or paused"]
        START["Start campaign"]
    end

    subgraph OUT["Outputs"]
        REP["CampaignProvisioningReport JSON"]
        HOOK["Outbound webhook<br/>Clay · n8n · Slack"]
        EXIT["Exit codes<br/>0 ok · 1 guardrail block · 2 error"]
    end

    STOP["⛔ Blocked<br/>no campaign changes · exit 1"]

    D1 --> G1
    D2 --> G2
    G1 -->|"no"| STOP
    G1 -->|"yes"| G2
    G2 -->|"no"| STOP
    G2 -->|"yes"| G3
    G3 -->|"no"| STOP
    G3 -->|"yes"| C1
    C1 --> C2
    C2 --> C3
    C3 --> C4
    C4 -->|"no, default"| DRAFT
    C4 -->|"yes"| START
    DRAFT --> REP
    START --> REP
    REP --> HOOK
    REP --> EXIT
```

| Area | Rule |
|---|---|
| **Domains** | Only Day 1 domains with `ready_to_send: true` and a daily cap above 0; the rest are listed in `excluded_domains` |
| **Inboxes** | Only Smartlead accounts on those domains with SMTP connected, warm-up `ACTIVE` and not blocked |
| **Leads** | Only Day 2 results with `status: valid`; risky, invalid, unknown and duplicate emails are skipped and counted |
| **Guardrails** | `no_ready_domains_available`, `no_valid_leads_to_upload`, `no_healthy_mailboxes_on_ready_domains` and `campaign_already_active` stop the run before any write, with exit `1` |
| **Existing campaign** | `--campaign-id` reuses it; if it is already `ACTIVE`, provisioning needs `--activate` so leads never start sending by surprise |
| **Activation** | Off by default: new campaigns stay `DRAFTED`; `--activate` sends `START` as the last step |
| **Capacity** | `total_daily_capacity` is the sum of Day 1 daily caps for the domains that got inboxes attached |
| **Partial failure** | If a write fails after the campaign exists, the error names the campaign ID and how many leads were uploaded |

| Run | Command |
|---|---|
| **Sample data** | `python -m gtm_engine.cli.provision_campaign --campaign-name "Q1 Outbound" --readiness-report fixtures/sample_readiness_report.json --verification-report fixtures/sample_verification_report.json` |
| **Live, stays drafted** | `SMARTLEAD_API_KEY=… python -m gtm_engine.cli.provision_campaign --mode live --campaign-name "Q1 Outbound" --readiness-report readiness.json --verification-report verification.json` |
| **Live, start sending** | Add `--activate` |
| **Add to an existing campaign** | Add `--campaign-id <id>` |
| **Send to a webhook** | Add `--webhook-url <url>` (or set `CAMPAIGN_WEBHOOK_URL`) |

Create the input reports with `check_readiness … > readiness.json` (Day 1) and `verify_leads … > verification.json` (Day 2).

## Layout

```text
src/gtm_engine/
  contracts/   Pydantic JSON contracts (deliverability, warmup, readiness, leads, campaign)
  ports/       Protocol interfaces the core depends on
  adapters/    Vendor/data-source implementations (DNS, warm-up, email verification, campaigns, webhook)
  cli/         audit_domains, evaluate_warmup, check_readiness, verify_leads, provision_campaign
fixtures/      Sample DNS, warm-up and lead data, plus sample Day 1 and Day 2 reports
tests/         pytest suite (all network calls mocked)
```

Other commands: `python -m gtm_engine.cli.audit_domains` (DNS gate only) and `python -m gtm_engine.cli.evaluate_warmup` (warm-up gate only, mock data).
