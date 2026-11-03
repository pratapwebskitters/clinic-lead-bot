# SPEC: AI Lead Responder for Dental Clinics

## Goal
Reply to new patient enquiries within 60 seconds, qualify them, share a booking link,
and alert the clinic owner for hot or urgent leads.

## Input: POST /webhook/lead
Header `X-Webhook-Secret` must match env `WEBHOOK_SECRET`.
JSON: `{ name, phone, email (optional), message, service (optional), clinic_id }`

## Pipeline
1. Validate secret and payload (clinic_id must match `^[A-Za-z0-9_-]{1,64}$`).
2. Normalize phone (keep digits and a leading +).
3. Duplicate check: same phone or email within 24h -> status `duplicate`, no reply.
4. Save lead (status `received`) BEFORE any LLM call.
5. Load clinic config from `clinics/{clinic_id}.yaml`.
6. LLM qualification -> JSON validated by Pydantic:
   `{ language, intent, score(hot|warm|cold|spam), urgent, needs_human, reason, reply }`
7. Safety layer on the reply (below).
8. Route:
   - spam: log only, no reply
   - hot / urgent: send reply + Telegram alert
   - warm: send reply, `follow_up_at = now + 24h`
   - cold: send info reply
9. Clinic `mode: shadow` -> draft goes to the owner on Telegram, NOT the lead.
   Owner approves via `POST /admin/leads/{id}/approve`.
10. Update the lead record (score, reply, status, replied_at).

## Safety rules (enforced in code)
- Reject replies with currency amounts not present in the clinic config.
- Reject: guarantee, cure, 100%, painless, and a list of medication terms.
- `urgent=true` forces `needs_human=true`.
- Replies must be 80 words or fewer.
- On rejection or LLM failure: send a safe template reply and alert a human.

## Failure behavior
LLM error, invalid JSON after one retry, or any exception: the lead stays saved,
status `needs_human`, Telegram alert sent. Never lose a lead.

## Non-goals (for now)
WhatsApp, Instagram, dashboards, multi-user auth, Telegram inline approve buttons.
