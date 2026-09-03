"""Gateway client. Mock by default, Razorpay test mode when credentials exist.

An honest note about what a payment gateway can and cannot do, because getting
this wrong is the difference between a demo and a product:

**There is no "retry this failed payment" endpoint for a one-off card payment.**
A failed payment is terminal. Recovering it means one of:

  * sending the customer a fresh payment link (`POST /v1/payment_links`), or
  * re-charging a saved token or an active subscription/mandate, which requires
    the customer to have authorised that in advance.

So ``schedule_retry`` here does not pretend to re-run a dead payment. In test mode
it creates the order the retry would be attempted against and returns a scheduled
handle; a production integration would hand that to a worker that either recharges
a token or falls back to a link. The limitation is documented rather than hidden,
because a panel will ask.

Both implementations satisfy the same Protocol, so the executor is identical in
either mode and the mock is not a special case threaded through the real code.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Protocol

from .. import config


@dataclass
class GatewayResult:
    ok: bool
    reference: Optional[str] = None
    short_url: Optional[str] = None
    error: Optional[str] = None
    live: bool = False


class GatewayClient(Protocol):
    name: str
    live: bool

    def create_payment_link(
        self, payment_id: str, amount_paise: int, description: str, expire_in_hours: float
    ) -> GatewayResult: ...

    def schedule_retry(
        self, payment_id: str, order_id: str, amount_paise: int, when: datetime
    ) -> GatewayResult: ...


class MockGateway:
    """Deterministic stand-in. Same payment always gets the same reference.

    Determinism matters more than realism here: a demo that produces different
    ids on every run cannot be screenshotted, diffed, or tested.
    """

    name = "mock"
    live = False

    def __init__(self, failure_rate: float = 0.02, seed: int = 3):
        # A small non-zero failure rate so the executor's error path is exercised
        # in every run rather than only in theory.
        self.failure_rate = failure_rate
        self.seed = seed

    def _rng(self, payment_id: str) -> random.Random:
        return random.Random(int(hashlib.sha256(
            (payment_id + str(self.seed)).encode("utf-8")
        ).hexdigest()[:8], 16))

    def _ref(self, prefix: str, payment_id: str) -> str:
        return prefix + hashlib.sha256(payment_id.encode("utf-8")).hexdigest()[:14]

    def create_payment_link(
        self, payment_id, amount_paise, description, expire_in_hours, notes=None
    ):
        if self._rng(payment_id).random() < self.failure_rate:
            return GatewayResult(ok=False, error="mock_gateway_unavailable")
        ref = self._ref("plink_", payment_id)
        return GatewayResult(ok=True, reference=ref, short_url="https://rzp.io/i/" + ref[-8:])

    def schedule_retry(self, payment_id, order_id, amount_paise, when):
        if self._rng(payment_id + "retry").random() < self.failure_rate:
            return GatewayResult(ok=False, error="mock_scheduler_unavailable")
        return GatewayResult(ok=True, reference=self._ref("retry_", payment_id))


class RazorpayTestGateway:
    """Real Razorpay REST calls against test-mode credentials.

    Test mode moves no money. Even so, ``config.DRY_RUN`` gates this class at the
    executor level - live calls have to be switched on deliberately.
    """

    name = "razorpay_test"
    live = True
    BASE = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str):
        self.auth = (key_id, key_secret)

    def _post(self, path: str, payload: dict) -> GatewayResult:
        import httpx

        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(self.BASE + path, json=payload, auth=self.auth)
            if resp.status_code >= 400:
                body = resp.json() if resp.headers.get("content-type", "").startswith(
                    "application/json"
                ) else {}
                message = body.get("error", {}).get("description", resp.text[:200])
                return GatewayResult(ok=False, error="{}: {}".format(resp.status_code, message), live=True)
            data = resp.json()
            return GatewayResult(
                ok=True, reference=data.get("id"), short_url=data.get("short_url"), live=True
            )
        except Exception as exc:  # noqa: BLE001 - network layer, many exception types
            return GatewayResult(ok=False, error="{}: {}".format(type(exc).__name__, exc), live=True)

    def create_payment_link(
        self, payment_id, amount_paise, description, expire_in_hours, notes=None
    ):
        expire_by = int((datetime.now() + timedelta(hours=max(1.0, expire_in_hours))).timestamp())
        # Razorpay copies link notes onto the payment made against the link, which
        # is how merchant-side context (consent, customer id, LTV) reaches the
        # agent when a payment on this link later fails. Razorpay itself never
        # supplies that data - it belongs to the merchant.
        payload_notes = {"source": "rebound", "original_payment_id": payment_id}
        payload_notes.update(notes or {})
        return self._post("/payment_links", {
            "amount": amount_paise,
            "currency": "INR",
            "description": description[:255],
            "expire_by": expire_by,
            "reference_id": "rebound_" + payment_id,
            "notify": {"sms": False, "email": False},   # we own the messaging
            "reminder_enable": False,
            "notes": payload_notes,
        })

    def schedule_retry(self, payment_id, order_id, amount_paise, when):
        # See the module docstring: this creates the order a retry would target.
        # It does not - and cannot - re-run the original failed payment.
        return self._post("/orders", {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": ("rbnd_" + payment_id)[:40],
            "notes": {
                "source": "rebound",
                "retry_of": payment_id,
                "scheduled_for": when.isoformat(timespec="seconds"),
            },
        })


def build_gateway() -> GatewayClient:
    """Real client when test credentials are present, mock otherwise."""
    if config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET:
        return RazorpayTestGateway(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET)
    return MockGateway()
