---
name: run-gtm-campaign
description: Run the GTM master pipeline (lead source -> email verification -> domain readiness gate -> Smartlead campaign provisioning) with python -m gtm_engine.cli.run_pipeline. Use when the user asks to run, stage, test or launch a cold-email campaign from Clay, Cargo, a Google Sheet, a CSV/JSON file or mock data.
---

# Run a GTM campaign

The master pipeline runs four stages in order and stops before any campaign change if a guardrail fails:

1. **Load leads** from `mock`, a `file` (.csv/.json from Clay, Cargo or a CRM) or a `gsheet` (Google Sheet URL or ID).
2. **Verify** them with the Day 2 waterfall (deduplicates; only `valid` leads continue).
3. **Check sending domains** with the Day 1 readiness gate (only `ready_to_send` domains with a cap above 0 continue).
4. **Provision** the Day 3 campaign: create or reuse it, attach healthy inboxes, upload valid leads. It stays **drafted** unless `--launch` is passed.

## 1. Confirm the inputs before running

State these back to the user in one short list and fill in anything missing by asking:

- **Source**: `--source-type mock|file|gsheet` and `--source-ref` (file path, or Google Sheet URL/ID). Not needed for `mock`.
- **Sending domains**: `--domains` (comma-separated). These are the user's own sending domains, not the prospects' domains.
- **Campaign**: `--campaign-name`, plus `--campaign-id` only if adding to an existing campaign. Re-running without `--campaign-id` creates a second campaign.
- **Mode**: `--mode mock` (default, offline, changes nothing) or `--mode live` (real MillionVerifier and Smartlead calls).

If the user has not said "live", use `--mode mock`. For a first live run, suggest a small file first.

## 2. Draft by default; launch only on an explicit request

- **Never pass `--launch`** unless the user explicitly asks to launch, start or activate sending in this conversation. "Run the campaign", "set it up" or "push the leads" mean draft.
- When the user does ask to launch, first run without `--launch` (or reuse the latest draft run) and summarize: valid leads to upload, risky/invalid/unknown skipped, ready vs blocked domains, and total daily capacity. Then confirm and re-run with `--launch --campaign-id <id from the draft run>` so no duplicate campaign is created.
- In live mode, an existing `--campaign-id` that is already `ACTIVE` is blocked unless `--launch` is passed; do not add `--launch` just to get past that block without asking.

## 3. Run it

From the repository root (desktop, or a mobile/web cloud session):

```bash
pip install -e ".[dev]"
```

Offline rehearsal with sample data:

```bash
python -m gtm_engine.cli.run_pipeline --source-type mock --domains healthy-outreach.com,quarantined-sender.com --campaign-name "Q1 RevOps Push"
```

A Clay or Cargo CSV export, staged as a draft in Smartlead:

```bash
python -m gtm_engine.cli.run_pipeline --source-type file --source-ref leads.csv --domains acme-mail.com,acme-outreach.com --campaign-name "Q1 RevOps Push" --mode live
```

A Google Sheet (shared as "Anyone with the link can view", or published as CSV):

```bash
python -m gtm_engine.cli.run_pipeline --source-type gsheet --source-ref "https://docs.google.com/spreadsheets/d/<sheet-id>/edit#gid=0" --domains acme-mail.com --campaign-name "Q1 RevOps Push" --mode live
```

Optional flags: `--selector <dkim-selector>` and `--tracking-host <subdomain>` for the readiness gate, `--output-dir <folder>`, and `--webhook-url <url>` (or `PIPELINE_WEBHOOK_URL`).

## 4. Keys and secrets

- Live mode needs `VERIFIER_API_KEY` (or `MILLIONVERIFIER_API_KEY`) and `SMARTLEAD_API_KEY`. Prefer environment variables or the cloud environment's secrets over command-line flags, so keys do not land in shell history.
- **Never print, echo, log or repeat an API key, token, webhook URL or private Google Sheet URL** in chat, commands you show, commit messages or files. Refer to them by variable name only (for example "`SMARTLEAD_API_KEY` is set"). To check that a key exists, use `test -n "$SMARTLEAD_API_KEY" && echo set`, never `echo $SMARTLEAD_API_KEY`.
- If a key is missing, the pipeline exits `2` naming the flag and variable it needs. Ask the user to set it; do not invent one.

## 5. Read the result

The run prints a JSON summary and saves it, with each stage's JSON, under `runs/<campaign-slug>/<UTC timestamp>/` (`01_raw_leads.json`, `02_verification_report.json`, `03_readiness_report.json`, `04_provisioning_report.json`, `run_summary.json`). The `runs/` folder holds lead data and is git-ignored; never commit it.

Report back in plain words: status, campaign ID and status (`DRAFTED` or `ACTIVE`), leads loaded / valid / uploaded, ready and blocked domains, daily capacity, and the run folder.

| Exit code | Meaning | What to tell the user |
|---|---|---|
| `0` | Provisioned | Campaign created or updated; say whether it is drafted or active. |
| `1` | Guardrail block, nothing changed in Smartlead | Quote the `guardrail:` reasons, e.g. no ready domains (list the `domain_blocked:` reasons) or no valid leads (give the `lead_counts:`). Suggest the fix; do not retry with different flags to force it through. |
| `2` | Error or missing config | Quote the `*_failed:` reason or the argument error: missing file, unreadable sheet (check sharing), bad arguments, missing key, or an API/network failure. If the summary status is `provisioned` but a webhook failed, the campaign exists: do not re-run without `--campaign-id`. |
