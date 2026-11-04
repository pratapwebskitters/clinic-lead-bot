# Clinic Lead Bot

Replies to new clinic enquiries within a minute, qualifies them (hot / warm / cold / spam),
shares a booking link, and alerts the owner on Telegram for hot or urgent leads.

Flow: `POST /webhook/lead` -> save lead -> LLM qualification -> safety checks -> route
(reply / alert / follow-up). Clinics start in **shadow mode**: drafts go to the owner, not the lead.

## Quick start

Bash (macOS/Linux):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Fill in `.env`. The app loads it automatically; existing environment variables take precedence.
`LLM_MODEL` may be blank to use the built-in default; verify the model name with your provider.
For Gemini, set `LLM_PROVIDER=gemini`, `LLM_MODEL=gemini-3.5-flash-lite`, and
`LLM_API_KEY` to your Google AI Studio API key. Gemini uses the same deadline,
response-size limit, validation, and safety checks as the other providers.
Use fictional enquiries when evaluating free-tier services; check the provider's data-use terms.
Set `APP_ENV=production` for production validation. `ADMIN_TELEGRAM_CHAT_ID` receives failure alerts.
`ENABLE_RECOVERY=1` (default) starts recovery and failed-alert retries in the background at startup
and every minute, independently of `ENABLE_SCHEDULER`. Only one instance should run recovery;
set `ENABLE_RECOVERY=0` on all other instances. `ENABLE_SCHEDULER=1` enables follow-ups only.
`LEAD_WORKERS=4` sets the maximum concurrent lead processors; the waiting queue is also bounded.
Startup does not wait for recovery or LLM calls. Each lead gets a shared 45-second processing
budget, including LLM retries and SMTP. If it expires before confirmed delivery, a checked safe
template gets one SMTP attempt with a maximum 15-second socket timeout, followed by human review.
Uncertain delivery is never resent automatically.

Run these commands in either Bash or PowerShell (tests mock outgoing messages):

```text
pytest
uvicorn app.main:app --reload
```

Send a test lead from a second terminal. Set these shell variables to the same secrets as `.env`;
loading `.env` in the server does not set variables in your terminal.

Bash:

```bash
export WEBHOOK_SECRET='your-webhook-secret'
export ADMIN_SECRET='your-admin-secret'
curl -X POST http://localhost:8000/webhook/lead \
  -H "X-Webhook-Secret: $WEBHOOK_SECRET" -H "Content-Type: application/json" \
  -d '{"name":"Priya","phone":"+919800000000","email":"you@example.com","message":"Want a root canal appointment tomorrow","clinic_id":"demo_clinic"}'
```

PowerShell:

```powershell
$env:WEBHOOK_SECRET = 'your-webhook-secret'
$env:ADMIN_SECRET = 'your-admin-secret'
$lead = @{
    name = 'Priya'
    phone = '+919800000000'
    email = 'you@example.com'
    message = 'Want a root canal appointment tomorrow'
    clinic_id = 'demo_clinic'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://localhost:8000/webhook/lead' `
    -Headers @{ 'X-Webhook-Secret' = $env:WEBHOOK_SECRET } `
    -ContentType 'application/json' -Body $lead
```

Approve a shadow-mode draft (sends it to the lead). Replace `1` with the returned lead ID.

Bash:

```bash
curl -X POST http://localhost:8000/admin/leads/1/approve -H "X-Admin-Secret: $ADMIN_SECRET"
```

PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://localhost:8000/admin/leads/1/approve' `
    -Headers @{ 'X-Admin-Secret' = $env:ADMIN_SECRET }
```

## Read results from n8n or a CRM

After submitting an enquiry, retain the returned `lead_id` alongside the Salesforce
Lead ID. Use an HTTP Request node to read `GET /admin/leads/{lead_id}`, with a
Header Auth credential named `X-Admin-Secret` whose value is `ADMIN_SECRET`.
For n8n running in Docker on this Mac, the URL is
`http://host.docker.internal:8010/admin/leads/{lead_id}`.

The authenticated, read-only response includes `status`, `score`, `reply`,
`needs_human`, `review_required`, `urgent`, `sent_at`, and `duplicate_of`. It never
approves or sends a reply. `received`, `processing`, and `sending` are in-progress
states: use a Wait node and poll again with a bounded loop. If the loop times out,
retain the waiting status in the CRM and reconcile it using a scheduled workflow;
do not resubmit the enquiry. A non-null `sent_at` records confirmed SMTP acceptance,
not guaranteed inbox delivery. `delivery_unknown` always requires human review,
even if a confirmed send was subsequently recorded.

Suggested Salesforce Lead custom fields:

| Field API name | Type | Response value |
|---|---|---|
| `Bot_Lead_ID__c` | Text (64) | `lead_id` |
| `Bot_Status__c` | Text (40) | `status` |
| `Bot_Score__c` | Text (10) | `score` (can be null) |
| `Bot_Needs_Human__c` | Checkbox | `review_required` |

Use the Salesforce ID from the earlier **Get a lead** node when updating the CRM;
the numeric bot ID is not a Salesforce record ID. Store the bot ID immediately
after submission so polling can resume after an interrupted workflow. Duplicate
submissions have their own ID and `duplicate_of` points to the original: retain the
duplicate status, and fetch the original separately if you need its qualification.
When `review_required` is true, route to a Salesforce Task linked through `WhoId`
to the Lead. Look for an existing review task for that bot ID before creating one
on subsequent polls. Do not automatically call the approval endpoint.

The status endpoint enables polling only; it does not push callbacks or
automatically update Salesforce. Keep periodic reconciliation enabled to discover
later recovery or follow-up escalations. CRM tasks also do not acknowledge the
app's existing Telegram alert queue; replacing Telegram delivery is a separate
integration.

## Project layout

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app: `/health`, `/webhook/lead`, `/admin/leads/{id}/approve` |
| `app/pipeline.py` | Orchestrates qualification, safety, routing; never loses a lead |
| `app/db.py` | SQLite layer (leads, duplicates, follow-ups, stats) |
| `app/models.py` | Pydantic models for payload and LLM result |
| `app/clinics.py` | Loads `clinics/<id>.yaml` (validated id, no path traversal) |
| `app/services/llm.py` | Prompt building, provider call (Anthropic/OpenAI via httpx), retry, fallback |
| `app/services/safety.py` | Code-level reply guardrails |
| `app/services/router.py` | Reply, owner alert, shadow mode, follow-up scheduling |
| `app/services/notify.py`, `mailer.py` | Telegram and SMTP senders (never raise) |
| `app/services/followup.py` | One follow-up for warm leads (APScheduler job) |
| `app/prompts/qualify.md` | System prompt |
| `clinics/demo_clinic.yaml` | Example clinic. **Demo data: replace before real use** |
| `scripts/weekly_report.py` | ROI report: leads, response time, scores, follow-ups |
| `tests/` | Unit, webhook and pipeline tests, plus the 25-lead real-LLM quality test |

## Onboarding a new clinic

1. Copy `clinics/demo_clinic.yaml` to `clinics/<new_id>.yaml` and fill it in. Only list prices the clinic approved.
2. Put the clinic's Telegram chat id in `owner_telegram_chat_id`.
3. Point the clinic's form at the webhook with `"clinic_id": "<new_id>"`.
4. Keep `mode: shadow` for the first 1-2 weeks, then switch to `auto`.

Every YAML file is validated at startup. Invalid files produce development warnings and
prevent production startup. Required fields are `clinic_name`, `booking_link` (HTTP/HTTPS),
`phone`, `location`, `timings`, `prices` (string values), and `mode` (`shadow` or `auto`).
Replace values beginning with `REPLACE` before using auto mode or production.
Production with any auto-mode clinic also requires `SMTP_HOST` and `SMTP_FROM`.
Filename stems must match `^[a-z0-9_-]{1,64}$`; use lowercase IDs and filenames on every OS.

## Quality test (real LLM, costs a little)

Bash:

```bash
RUN_LLM_TESTS=1 pytest tests/test_quality.py -s
```

PowerShell:

```powershell
$env:RUN_LLM_TESTS = '1'
try {
    pytest tests/test_quality.py -s
} finally {
    Remove-Item Env:RUN_LLM_TESTS
}
```

Targets: 85%+ score accuracy and zero rule violations. Edit `app/prompts/qualify.md`,
change one thing at a time, re-run.

## Deploy

Build and run with Docker in either Bash or PowerShell after configuring `.env`:

```text
docker build -t clinic-lead-bot .
docker run --env-file .env -e APP_ENV=production -p 8000:8000 -v clinic-leads:/data -e DB_PATH=/data/leads.db clinic-lead-bot
```

`APP_ENV=production` makes startup fail clearly if required variables are missing.
Point uptime monitoring at `/health`.

Important: SQLite lives in a file. On hosts with ephemeral disks (Render free tier, many PaaS)
the file is wiped on redeploy. Attach a persistent disk and set `DB_PATH` to it, or move to Postgres.
Set `ENABLE_SCHEDULER=1` on exactly one instance to run follow-ups.
Keep `ENABLE_RECOVERY=1` on exactly one instance, even when follow-ups are disabled.

## Known limits (next steps)

- Replies go out by email only. WhatsApp/Instagram: add a `/webhook/whatsapp` that builds the same lead
  model, and a sender behind `router.deliver_reply`.
- Shadow-mode approval is an HTTP endpoint, not Telegram buttons yet.
- No "booked" webhook yet (the `booked` column exists; a Cal.com webhook should set it to stop follow-ups).
- Replies are not guaranteed correct: keep shadow mode on until you've reviewed real drafts.
- Health data: collect minimum details, add consent text to the form, and check DPDP Act obligations.
