"""Generates a realistic batch of failed payments with hidden ground-truth labels.

Two things matter here and both are deliberate:

1. A share of rows carry NO structured error_reason - only a messy free-text bank
   or PSP string. That is what real gateway traffic looks like once you get past
   the top few issuers, and it is the only honest justification for putting an
   LLM in this pipeline at all. Everything else is handled by rules.

2. Every row has a hidden ``true_class`` written to a separate labels file, so the
   classifier can be scored on held-out data instead of graded by vibes.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

from ..models import FailedPayment
from ..taxonomy import PROFILES, FailureClass, Rail

# (error_code, error_reason, description) templates per class.
STRUCTURED: Dict[FailureClass, List[Tuple[str, str, str]]] = {
    FailureClass.INSUFFICIENT_FUNDS: [
        ("BAD_REQUEST_ERROR", "insufficient_funds", "Your account has insufficient balance"),
        ("GATEWAY_ERROR", "payment_failed", "Insufficient funds in the customer account"),
    ],
    FailureClass.BANK_DOWNTIME: [
        ("GATEWAY_ERROR", "issuer_down", "The bank is not responding, please retry later"),
        ("GATEWAY_ERROR", "gateway_timeout", "Issuer unavailable at the moment"),
    ],
    FailureClass.AUTH_DROPOFF: [
        ("BAD_REQUEST_ERROR", "payment_timeout", "Payment was not completed in time"),
        ("BAD_REQUEST_ERROR", "authentication_failed", "OTP was not entered by the customer"),
    ],
    FailureClass.EXPIRED_INSTRUMENT: [
        ("BAD_REQUEST_ERROR", "card_expired", "The card used has expired"),
    ],
    FailureClass.INVALID_INSTRUMENT: [
        ("BAD_REQUEST_ERROR", "invalid_vpa", "The VPA entered does not exist"),
        ("BAD_REQUEST_ERROR", "invalid_card", "Card number is invalid or account is closed"),
    ],
    FailureClass.LIMIT_EXCEEDED: [
        ("BAD_REQUEST_ERROR", "limit_exceeded", "Transaction exceeds the per-transaction limit"),
    ],
    FailureClass.RISK_DECLINE_ISSUER: [
        ("BAD_REQUEST_ERROR", "risk_threshold_breached", "Declined by issuer risk engine"),
    ],
    FailureClass.SUSPECTED_FRAUD: [
        ("BAD_REQUEST_ERROR", "fraudulent", "Payment blocked - suspected fraudulent activity"),
    ],
    FailureClass.MANDATE_INACTIVE: [
        ("BAD_REQUEST_ERROR", "mandate_revoked", "The e-mandate has been revoked by the customer"),
        ("BAD_REQUEST_ERROR", "mandate_not_active", "Mandate is paused or not yet active"),
    ],
    FailureClass.TECHNICAL_ERROR: [
        ("GATEWAY_ERROR", "gateway_technical_error", "Technical error at the payment gateway"),
    ],
    FailureClass.CUSTOMER_CANCELLED: [
        ("BAD_REQUEST_ERROR", "payment_cancelled_by_user", "Customer cancelled the payment"),
    ],
}

# The unstructured tail: strings issuers and PSPs actually emit, with no reason code.
# Several of these are genuinely ambiguous to a human too - that is the point.
UNSTRUCTURED: Dict[FailureClass, List[str]] = {
    FailureClass.INSUFFICIENT_FUNDS: [
        "DECLINE - insufficient balance available in the linked account",
        "U31: debit has failed at the remitter bank",
        "Do not honour. Balance low.",
    ],
    FailureClass.BANK_DOWNTIME: [
        "RC 91 - issuer inoperative, try after some time",
        "Remitter bank is currently under scheduled maintenance window",
        "NPCI: unable to reach beneficiary PSP, timed out",
    ],
    FailureClass.AUTH_DROPOFF: [
        "collect request expired without customer approval",
        "3DS session abandoned by cardholder before completion",
        "user did not authorise the mandate within the window",
    ],
    FailureClass.EXPIRED_INSTRUMENT: [
        "Card no longer valid - reissued by bank",
    ],
    FailureClass.INVALID_INSTRUMENT: [
        "handle not registered with any PSP",
        "account closed at the issuing branch",
    ],
    FailureClass.LIMIT_EXCEEDED: [
        "per day cap for this instrument already utilised",
    ],
    FailureClass.RISK_DECLINE_ISSUER: [
        "Declined by bank. Please contact your card issuer.",
        "transaction not permitted to cardholder",
    ],
    FailureClass.SUSPECTED_FRAUD: [
        "pick up card - suspected compromise",
    ],
    FailureClass.MANDATE_INACTIVE: [
        "standing instruction no longer registered against this account",
    ],
    FailureClass.TECHNICAL_ERROR: [
        "upstream 502 while confirming the debit, state indeterminate",
        "System malfunction at acquirer. No debit occurred.",
    ],
    FailureClass.CUSTOMER_CANCELLED: [
        "closed the checkout window",
    ],
}

RAIL_BY_CLASS: Dict[FailureClass, List[Rail]] = {
    FailureClass.MANDATE_INACTIVE: [Rail.EMANDATE],
    FailureClass.EXPIRED_INSTRUMENT: [Rail.CARD],
    FailureClass.INVALID_INSTRUMENT: [Rail.UPI, Rail.CARD],
}
DEFAULT_RAILS = [Rail.UPI, Rail.UPI, Rail.UPI, Rail.CARD, Rail.CARD, Rail.NETBANKING, Rail.WALLET]

MERCHANTS = ["mrch_edtech", "mrch_d2c_apparel", "mrch_saas", "mrch_grocery"]


def generate_batch(
    n: int = 400,
    seed: int = 7,
    unstructured_share: float = 0.30,
    start: datetime = datetime(2026, 8, 25, 9, 0, 0),
) -> Tuple[List[FailedPayment], Dict[str, FailureClass]]:
    """Returns (payments, hidden truth labels keyed by payment_id)."""
    rng = random.Random(seed)
    classes = [c for c in PROFILES if c is not FailureClass.UNKNOWN]
    weights = [PROFILES[c].typical_share for c in classes]

    payments: List[FailedPayment] = []
    labels: Dict[str, FailureClass] = {}

    for i in range(n):
        true_class = rng.choices(classes, weights=weights, k=1)[0]
        pid = "pay_R{:07d}".format(1000000 + i)

        if rng.random() < unstructured_share and UNSTRUCTURED.get(true_class):
            code = rng.choice(["GATEWAY_ERROR", "BAD_REQUEST_ERROR"])
            reason = None
            desc = rng.choice(UNSTRUCTURED[true_class])
        else:
            code, reason, desc = rng.choice(STRUCTURED[true_class])

        rail = rng.choice(RAIL_BY_CLASS.get(true_class, DEFAULT_RAILS))
        amount = int(rng.choice([1, 1, 1, 2, 4, 9, 20]) * rng.randint(4900, 89900))
        prior = rng.choice([0, 0, 1, 2, 5, 11])

        payments.append(
            FailedPayment(
                payment_id=pid,
                order_id="order_R{:07d}".format(2000000 + i),
                customer_id="cust_{:05d}".format(rng.randint(1, max(2, n // 3))),
                merchant_id=rng.choice(MERCHANTS),
                amount=amount,
                rail=rail,
                method_detail=rng.choice(
                    ["HDFC credit", "SBI debit", "okicici", "ybl", "paytm", None]
                ),
                error_code=code,
                error_reason=reason,
                error_description=desc,
                created_at=start + timedelta(minutes=rng.randint(0, 60 * 72)),
                attempt_number=rng.choice([1, 1, 1, 2]),
                is_recurring=(rail is Rail.EMANDATE) or rng.random() < 0.12,
                customer_ltv_paise=prior * rng.randint(50000, 400000),
                prior_success_count=prior,
                contact_consent={
                    "whatsapp": rng.random() < 0.72,
                    "sms": rng.random() < 0.95,
                    "email": rng.random() < 0.88,
                },
            )
        )
        labels[pid] = true_class

    return payments, labels


def write_batch(out_dir: Path, n: int = 400, seed: int = 7) -> Tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    payments, labels = generate_batch(n=n, seed=seed)
    pay_path = out_dir / "failed_payments.jsonl"
    lab_path = out_dir / "labels.json"
    with pay_path.open("w", encoding="utf-8") as fh:
        for p in payments:
            fh.write(p.model_dump_json() + "\n")
    lab_path.write_text(
        json.dumps({k: v.value for k, v in labels.items()}, indent=2), encoding="utf-8"
    )
    return pay_path, lab_path


def load_batch(data_dir: Path) -> Tuple[List[FailedPayment], Dict[str, FailureClass]]:
    pay_path = data_dir / "failed_payments.jsonl"
    lab_path = data_dir / "labels.json"
    payments = [
        FailedPayment.model_validate_json(line)
        for line in pay_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    raw = json.loads(lab_path.read_text(encoding="utf-8"))
    return payments, {k: FailureClass(v) for k, v in raw.items()}
