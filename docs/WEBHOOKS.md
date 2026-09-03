# Wiring Rebound to real Razorpay webhooks

Rebound runs fine without this — `simulate-webhook` signs events with your real
secret and exercises the genuine verification path. This guide is for when you
want the loop closed against Razorpay's own infrastructure: a real failed payment
arriving as a real webhook, and a real recovery coming back.

Everything here is **test mode**. No money moves at any point.

---

## What you are building

```
  customer fails a payment
            │
            ▼
  Razorpay ──── payment.failed ────►  POST /webhooks/razorpay
                                              │
                                       verify HMAC signature
                                              │
                                       diagnose → decide → act
                                              │
                                       create payment link
            ◄──────────────────────────────────┘
            │
  customer pays the link
            │
            ▼
  Razorpay ──── payment_link.paid ──►  outcome recorded as OBSERVED
```

The second half is the point. `payment_link.paid` is the only event in the system
that produces a *measured* recovery rather than a modelled one.

---

## Step 1 — Start the receiver

```bash
cd d:/razorpay
python rebound.py serve
```

Confirm it is healthy and that the secret is loaded:

```bash
curl -s http://127.0.0.1:8000/health
```

`"webhook_secret_configured": true` must appear. If it says `false`, add
`RAZORPAY_WEBHOOK_SECRET` to `.env` and restart — the receiver returns `503`
rather than trusting an unverifiable event.

---

## Step 2 — Give it a public HTTPS address

Razorpay cannot reach `localhost`. You need a tunnel.

**Option A — Cloudflare Tunnel (no account needed).**

```bash
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```

It prints a `https://something-random.trycloudflare.com` URL. That is your public
address.

**Option B — ngrok** (requires a free account and `ngrok config add-authtoken`):

```bash
ngrok http 8000
```

Leave the tunnel running. The URL changes every restart on both free tiers, so if
you restart it you must update the webhook in the dashboard.

---

## Step 3 — Create the webhook in the Razorpay dashboard

1. Open [dashboard.razorpay.com](https://dashboard.razorpay.com)
2. **Switch to Test Mode** using the toggle at the top. Do this first.
3. Go to **Account & Settings → Webhooks → Add New Webhook**
4. Fill it in:

   | Field | Value |
   |---|---|
   | Webhook URL | `https://<your-tunnel>/webhooks/razorpay` |
   | Secret | the exact string in your `.env` as `RAZORPAY_WEBHOOK_SECRET` |
   | Alert Email | your email |

5. Tick these events and no others:
   - `payment.failed` — the trigger
   - `payment_link.paid` — the outcome
   - `payment.captured` — the outcome, for non-link recoveries

6. **Create Webhook.**

The secret is the HMAC key. If the dashboard value and the `.env` value differ by
so much as a space, every event will be rejected with `invalid signature` — which
is the receiver working correctly, not a bug.

---

## Step 4 — Generate a real failed payment

This is the part most guides skip. Razorpay test mode has dedicated handles that
force an outcome:

| What you want | What to enter at checkout |
|---|---|
| A failed payment | UPI ID `failure@razorpay` |
| A successful payment | UPI ID `success@razorpay` |
| A successful card payment | `4111 1111 1111 1111`, any future expiry, any CVV |

So:

```bash
# creates a real test-mode payment link and prints its URL
python rebound.py verify-gateway
```

Open the printed `https://rzp.io/...` link, choose **UPI**, enter
`failure@razorpay`, and submit.

Razorpay fires `payment.failed` at your tunnel. Watch the server log: Rebound
classifies the failure, prices the options, applies the guardrails, and decides.

Then open the same link again and pay with `success@razorpay`. That fires
`payment_link.paid`, and the recovery lands in the ledger as **observed**.

---

## Step 5 — Check what actually happened

```bash
# every webhook received, including any that failed verification
curl -s "http://127.0.0.1:8000/webhooks/recent?limit=10"

# the full reasoning for one payment
curl -s "http://127.0.0.1:8000/decisions/pay_XXXXXXXXXXXX"

# observed vs simulated outcomes
curl -s "http://127.0.0.1:8000/summary?run_id=run_live"
```

The last one is the one worth looking at. `source: "observed"` rows are measured.
`source: "simulated"` rows come from the harness. They are stored separately on
purpose and nothing in this repo merges them.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `503 RAZORPAY_WEBHOOK_SECRET is not configured` | Secret missing from `.env`, or the server was started before you added it |
| `400 invalid signature` on every event | Dashboard secret and `.env` secret do not match exactly |
| Dashboard shows delivery failures | Tunnel died, or its URL changed on restart |
| Events arrive but nothing is decided | Check `/webhooks/recent` — an `error` on the row means the handler faulted after acknowledging |
| Nothing arrives at all | Webhook was created in Live Mode, not Test Mode |

---

## A note on demoing this

For a recorded demo, `simulate-webhook` is the better choice:

```bash
python rebound.py simulate-webhook                          # valid event
python rebound.py simulate-webhook --bad-signature          # rejected
python rebound.py simulate-webhook --event-id evt_1         # first delivery
python rebound.py simulate-webhook --event-id evt_1         # deduplicated
python rebound.py simulate-webhook --event payment_link.paid
```

It signs with the same secret and goes through the same verification code, so it
proves the same things — but it is deterministic, needs no tunnel, and cannot fail
live on camera because someone's wifi dropped. The forged-signature rejection is a
better thirty seconds of video than a successful delivery anyway.
