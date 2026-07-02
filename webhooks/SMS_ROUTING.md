# SMS Routing — Webhooks service

How the webhooks service sends the client SMS for `POST /notify`. SMS now routes
through **Marketing Hub (HighLevel)** by default, with Twilio and a no-network
stub as fallbacks. All of it lives in `senders.py` (`deliver_sms`).

## Three-tier routing

`deliver_sms(to_phone, message) -> (channel, sent)` picks the first configured
channel, in this order:

| Order | Channel | Chosen when | `sent` is True when | `channel` |
|---|---|---|---|---|
| 1 | **HighLevel** | `HIGHLEVEL_WEBHOOK_URL` is set | the webhook returns 2xx | `"highlevel"` |
| 2 | **Twilio** | HighLevel unset **and** all `TWILIO_*` set | Twilio POST issued | `"twilio"` |
| 3 | **Stub** | nothing configured | never (`False`) | `"stub"` |

The stub makes no network call — it logs the message it *would* have sent and
returns `False`, so the service runs end-to-end in dev with no messaging account.

```
HIGHLEVEL_WEBHOOK_URL set? ──yes──▶ POST JSON to HighLevel ──▶ ("highlevel", 2xx?)
        │no
        ▼
TWILIO_* all set? ──yes──▶ POST to Twilio Messages API ──▶ ("twilio", True)
        │no
        ▼
   ("stub", False)   # logged, no network
```

## HighLevel payload contract

`send_via_highlevel` POSTs exactly this JSON body (`Content-Type: application/json`,
15s timeout) to `HIGHLEVEL_WEBHOOK_URL`:

```json
{ "phone": "+15555550123", "message": "Vance Credit: your credit report improved this cycle. True to our promise, you're only charged when it moves - so your $99 fee for June 2026 was applied. See the details anytime in your client portal: https://vancecredit.com" }
```

Returns `True` only on a 2xx response; any non-2xx, network error, or timeout
returns `False`.

**HighLevel workflow side (Marketing Hub):** the inbound webhook trigger maps
`phone` → contact (match/create by phone) and `message` → the **Send SMS** action,
sent **from the verified toll-free number**. Keep the workflow's field names in
sync with this `{phone, message}` contract.

## Environment variables

| Var | Used by | Meaning |
|---|---|---|
| `HIGHLEVEL_WEBHOOK_URL` | `send_via_highlevel` / `deliver_sms` | Marketing Hub inbound webhook URL. Set → HighLevel is the SMS channel. |
| `TWILIO_ACCOUNT_SID` | `send_sms` | Twilio account SID (fallback channel). |
| `TWILIO_AUTH_TOKEN` | `send_sms` | Twilio auth token. |
| `TWILIO_FROM` | `send_sms` | Twilio sender number. Twilio is used only if **all three** `TWILIO_*` are set. |
| `PORTAL_URL` | `messages.py` | Client portal **login URL** linked in every receipt / payment-failed SMS. Defaults to `https://vancecredit.com`; set it to the real portal login page. |

(Unrelated to SMS: `CRC_INVOICE_URL` drives `POST /crc/invoice`.)

## Skip suppression

`POST /notify` with `type == "skipped"` is **never texted**. The handler
short-circuits before composing/sending and returns:

```json
{ "ok": true, "channel": "none", "sent": false, "note": "skip not texted" }
```

Rationale: pinging a client about a *non*-charge on a free-month cycle is poor UX
and needlessly burns the toll-free messaging ramp. Only `receipt` and
`payment_failed` produce an SMS.

## Message catalog

All copy lives in `messages.py` (`compose()` routes by `type`). Amounts render via
`_money()`, the period via `cycle_label()` (`"2026-06"` → `"June 2026"`), and the
link via `PORTAL_URL`.

| `type` | Trigger | Texted? | Template |
|---|---|---|---|
| `receipt` | charge succeeded (report moved this cycle) | ✅ yes | `Vance Credit: your credit report improved this cycle. True to our promise, you're only charged when it moves - so your $<amount> fee for <Month YYYY> was applied. See the details anytime in your client portal: <PORTAL_URL>` |
| `payment_failed` | report moved but the card was declined | ✅ yes | `Vance Credit: your credit report improved this cycle, but we couldn't process your $<amount> fee for <Month YYYY>. Update your card in your client portal so we can keep working on your file: <PORTAL_URL>` |
| `skipped` | free-month cycle (no movement, no charge) | ❌ no — logged only | `Vance Credit: no changes posted to your report this cycle - no charge. Your disputes continue next round.` |

Rendered examples (`amount=99`, `cycle="2026-06"`, `PORTAL_URL=https://portal.example.com`):

- **receipt** (227 chars, 2 segments): `Vance Credit: your credit report improved this cycle. True to our promise, you're only charged when it moves - so your $99 fee for June 2026 was applied. See the details anytime in your client portal: https://portal.example.com`
- **payment_failed** (208 chars, 2 segments): `Vance Credit: your credit report improved this cycle, but we couldn't process your $99 fee for June 2026. Update your card in your client portal so we can keep working on your file: https://portal.example.com`
- **skipped** (105 chars, 1 segment): `Vance Credit: no changes posted to your report this cycle - no charge. Your disputes continue next round.`

## Message formatting rules (enforced by tests)

- **Pure ASCII, no em-dash.** Templates use a plain `" - "` separator — this
  removed the `â` mojibake seen on some carriers/gateways. Tests assert every
  message `.encode("ascii")` cleanly and contains no `—`, no double spaces, and
  no `"$"` after a digit.
- **Money via `_money()`:** whole amounts drop decimals (`99.0 → "99"`),
  fractional amounts keep two places (`149.5 → "149.50"`) — always `"$99"`, never `"99$"`.
- **2-segment budget.** `sms_segments()` counts GSM-7 concatenated segments
  (1 if ≤160 chars, else `ceil(len/153)`), and every message is asserted ≤ 2.
  The budget is deliberately conservative: we're on the **toll-free Level-1
  messaging ramp** (low daily throughput while unverified/early), and **HighLevel
  appends an opt-out line** (e.g. "Reply STOP to opt out") to the body it sends,
  which consumes part of the 2nd segment. Staying ≤ 2 keeps the delivered message
  from spilling into a 3rd segment and protects deliverability during the ramp.
