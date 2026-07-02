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
{ "phone": "+15555550123", "message": "Vance Credit: your report moved this cycle - $99 for 2026-06. Details in your portal." }
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

## Message formatting notes

- SMS templates use a plain `" - "` separator (never an em-dash), which removed
  the `â` mojibake seen on some carriers/gateways.
- Dollar amounts render via `_money()`: whole amounts drop the decimals
  (`99.0 → "99"`) and fractional amounts keep two places (`149.5 → "149.50"`).
