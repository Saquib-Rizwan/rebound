"""Razorpay webhook receipt: verification, parsing, and closing the outcome loop.

This is how the agent actually connects to payments. Batch files are a fine way to
evaluate a policy, but a merchant does not hand you a JSONL - Razorpay pushes an
event the moment something happens, and there are exactly two that matter here:

  payment.failed        a payment just died. This is the trigger. The agent
                        diagnoses it and decides what, if anything, to do.
  payment_link.paid     someone paid a link we sent. This is the *outcome*, and
                        it is the single most valuable event in the system,
                        because it turns a simulated recovery into an observed
                        one. Every number in reports/recovery.md is modelled;
                        every number derived from this event is measured.

Security notes, none of which are optional:

* The signature is an HMAC-SHA256 hex digest over the **raw request body**. Parsing
  the JSON and re-serialising it changes the bytes and the signature will not
  match. The endpoint therefore reads bytes and never touches the parsed body
  before verifying.
* Comparison is constant-time. A `==` on a signature is a timing oracle.
* Razorpay retries webhooks, so delivery is at-least-once. `x-razorpay-event-id`
  is the dedup key, and it is stored, not just checked in memory - a process
  restart must not cause a duplicate action.
* An unverified event is recorded and rejected. It is never processed, but it is
  also never silently dropped, because a burst of bad signatures is a signal.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from ..models import FailedPayment
from ..taxonomy import Rail

SIGNATURE_HEADER = "x-razorpay-signature"
EVENT_ID_HEADER = "x-razorpay-event-id"

# Events we act on. Anything else is stored and acknowledged, not processed -
# subscribing to a new event type in the dashboard must never crash the receiver.
TRIGGER_EVENTS = frozenset({"payment.failed"})
OUTCOME_EVENTS = frozenset({"payment_link.paid", "payment.captured", "order.paid"})

METHOD_TO_RAIL = {
    "card": Rail.CARD,
    "upi": Rail.UPI,
    "netbanking": Rail.NETBANKING,
    "wallet": Rail.WALLET,
    "emandate": Rail.EMANDATE,
    "nach": Rail.EMANDATE,
}


def compute_signature(raw_body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, signature: Optional[str], secret: str) -> bool:
    """Constant-time HMAC check over the raw bytes Razorpay actually sent."""
    if not signature or not secret:
        return False
    return hmac.compare_digest(compute_signature(raw_body, secret), signature)


def _entity(event: Dict[str, Any], name: str) -> Dict[str, Any]:
    return event.get("payload", {}).get(name, {}).get("entity", {}) or {}


def event_payment_id(event: Dict[str, Any]) -> Optional[str]:
    """The id of the payment this event is *about*, from our point of view.

    Order matters here and getting it wrong is subtle. Paying a recovery link
    creates a **new** payment with a new id - it is not the failed payment coming
    back to life. So `payment_link.paid` carries two identities, and the one we
    care about is the original failure, which we stamped into the link's
    `reference_id` when we created it.

    Reading `payment.id` first looks correct and silently files every recovery
    against a payment we have never seen, orphaned from the decision that earned
    it. See POSTMORTEM entry 6 - it only surfaced against live Razorpay.
    """
    link = _entity(event, "payment_link")
    reference = link.get("reference_id") or ""
    if reference.startswith("rebound_"):
        return reference[len("rebound_"):]

    payment = _entity(event, "payment")
    notes = payment.get("notes") or {}
    # Same idea for a captured payment carrying our marker in its notes.
    if notes.get("retry_of"):
        return str(notes["retry_of"])
    if payment.get("id"):
        return payment["id"]
    return None


def parse_payment_failed(event: Dict[str, Any]) -> FailedPayment:
    """Maps a Razorpay payment.failed entity onto our domain model.

    Fields Razorpay does not send - customer lifetime value, prior successes,
    messaging consent - are the merchant's own data. In a real deployment they
    come from the merchant's CRM; here they default conservatively, which is the
    safe direction: no consent means no contact.
    """
    payment = _entity(event, "payment")
    notes = payment.get("notes") or {}

    created = payment.get("created_at")
    created_at = (
        datetime.fromtimestamp(created, tz=timezone.utc).replace(tzinfo=None)
        if isinstance(created, (int, float))
        else datetime.utcnow()
    )

    consent = {
        "whatsapp": str(notes.get("consent_whatsapp", "")).lower() == "true",
        "sms": str(notes.get("consent_sms", "")).lower() == "true",
        "email": str(notes.get("consent_email", "")).lower() == "true",
    }

    return FailedPayment(
        payment_id=payment.get("id") or "pay_unknown",
        order_id=payment.get("order_id") or "order_unknown",
        customer_id=(
            payment.get("customer_id")
            or notes.get("customer_id")
            or payment.get("email")
            or "cust_unknown"
        ),
        merchant_id=notes.get("merchant_id") or event.get("account_id") or "mrch_unknown",
        amount=int(payment.get("amount") or 0),
        currency=payment.get("currency") or "INR",
        rail=METHOD_TO_RAIL.get(payment.get("method") or "", Rail.CARD),
        method_detail=payment.get("bank") or payment.get("wallet") or payment.get("vpa"),
        error_code=payment.get("error_code"),
        error_reason=payment.get("error_reason"),
        error_description=payment.get("error_description"),
        created_at=created_at,
        attempt_number=int(notes.get("attempt_number", 1) or 1),
        is_recurring=bool(payment.get("invoice_id")) or payment.get("method") == "emandate",
        customer_ltv_paise=int(notes.get("ltv_paise", 0) or 0),
        prior_success_count=int(notes.get("prior_successes", 0) or 0),
        contact_consent=consent,
    )


def parse_outcome(event: Dict[str, Any]) -> Tuple[Optional[str], int]:
    """Returns (original_payment_id, amount_paise) for a successful payment."""
    payment_id = event_payment_id(event)
    payment = _entity(event, "payment")
    link = _entity(event, "payment_link")
    amount = int(payment.get("amount") or link.get("amount_paid") or link.get("amount") or 0)
    return payment_id, amount


def summarise(raw_body: bytes) -> Dict[str, Any]:
    """Light parse for logging, safe on malformed bodies."""
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"event": "unparseable", "payment_id": None}
    return {"event": event.get("event"), "payment_id": event_payment_id(event)}
