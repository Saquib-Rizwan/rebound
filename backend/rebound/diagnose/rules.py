"""Deterministic classifier. Runs first, and handles the majority for free.

Design rule: **high precision, freely abstaining recall.** A rule fires only when
the evidence is unambiguous. Anything a careful human would hesitate over returns
None and falls through to the model. That trade is deliberate - a confident wrong
class here becomes a wrong intervention downstream, and a wrong intervention costs
real money, whereas an abstention only costs one cheap LLM call.

This module is the answer to the "use deterministic solutions where AI is
unnecessary" bar. We measure exactly how far it gets us in reports/classifier.md.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ..models import Diagnosis, FailedPayment
from ..taxonomy import FailureClass

# Structured reason codes are contract, not prose - trust them fully.
REASON_MAP: Dict[str, FailureClass] = {
    "insufficient_funds": FailureClass.INSUFFICIENT_FUNDS,
    "issuer_down": FailureClass.BANK_DOWNTIME,
    "gateway_timeout": FailureClass.BANK_DOWNTIME,
    "payment_timeout": FailureClass.AUTH_DROPOFF,
    "authentication_failed": FailureClass.AUTH_DROPOFF,
    "card_expired": FailureClass.EXPIRED_INSTRUMENT,
    "invalid_vpa": FailureClass.INVALID_INSTRUMENT,
    "invalid_card": FailureClass.INVALID_INSTRUMENT,
    "limit_exceeded": FailureClass.LIMIT_EXCEEDED,
    "risk_threshold_breached": FailureClass.RISK_DECLINE_ISSUER,
    "fraudulent": FailureClass.SUSPECTED_FRAUD,
    "mandate_revoked": FailureClass.MANDATE_INACTIVE,
    "mandate_not_active": FailureClass.MANDATE_INACTIVE,
    "gateway_technical_error": FailureClass.TECHNICAL_ERROR,
    "payment_cancelled_by_user": FailureClass.CUSTOMER_CANCELLED,
    # Deliberately absent: "payment_failed". It is the gateway's catch-all and
    # carries no root cause, so we let the description decide instead of guessing.
}

# High-precision text anchors, tried only when there is no usable reason code.
# Order matters: the first match wins, so the most specific phrases come first.
TEXT_RULES: List[Tuple[str, FailureClass, str]] = [
    (r"insufficient\s+(balance|funds)", FailureClass.INSUFFICIENT_FUNDS, "txt_insufficient"),
    (r"\bbalance\s+low\b", FailureClass.INSUFFICIENT_FUNDS, "txt_balance_low"),
    (r"\brc\s*91\b|issuer\s+inoperative", FailureClass.BANK_DOWNTIME, "txt_rc91"),
    (r"maintenance\s+window", FailureClass.BANK_DOWNTIME, "txt_maintenance"),
    (r"\bnot\s+responding\b", FailureClass.BANK_DOWNTIME, "txt_not_responding"),
    (r"abandoned|did\s+not\s+authorise|expired\s+without", FailureClass.AUTH_DROPOFF, "txt_abandoned"),
    (r"otp\s+was\s+not\s+entered", FailureClass.AUTH_DROPOFF, "txt_no_otp"),
    (r"no\s+longer\s+valid|reissued\s+by\s+bank|has\s+expired", FailureClass.EXPIRED_INSTRUMENT, "txt_expired"),
    (r"not\s+registered\s+with\s+any\s+psp|account\s+closed", FailureClass.INVALID_INSTRUMENT, "txt_closed"),
    (r"does\s+not\s+exist", FailureClass.INVALID_INSTRUMENT, "txt_no_vpa"),
    (r"per\s+day\s+cap|exceeds\s+the\s+per-transaction\s+limit", FailureClass.LIMIT_EXCEEDED, "txt_cap"),
    (r"pick\s+up\s+card|suspected\s+(compromise|fraud)", FailureClass.SUSPECTED_FRAUD, "txt_fraud"),
    (r"standing\s+instruction\s+no\s+longer|mandate\s+is\s+paused", FailureClass.MANDATE_INACTIVE, "txt_si_gone"),
    (r"system\s+malfunction|technical\s+error\s+at", FailureClass.TECHNICAL_ERROR, "txt_malfunction"),
    (r"closed\s+the\s+checkout|cancelled\s+the\s+payment", FailureClass.CUSTOMER_CANCELLED, "txt_cancelled"),
    (r"risk\s+engine", FailureClass.RISK_DECLINE_ISSUER, "txt_risk_engine"),
]

_COMPILED_TEXT = [(re.compile(p, re.IGNORECASE), fc, rid) for p, fc, rid in TEXT_RULES]

# Phrases we know are ambiguous. Listing them is documentation: these are the rows
# the model exists to handle, and naming them stops anyone "fixing" recall later
# by bolting on a regex that would be wrong a third of the time.
KNOWN_AMBIGUOUS = [
    "declined by bank",
    "do not honour",
    "transaction not permitted",
    "u30",
    "u31",
    "unable to reach",
    "indeterminate",
]


def classify(payment: FailedPayment, clean_description: str = "") -> Optional[Diagnosis]:
    """Returns a Diagnosis when the evidence is unambiguous, else None."""
    reason = (payment.error_reason or "").strip().lower()
    if reason in REASON_MAP:
        return Diagnosis(
            payment_id=payment.payment_id,
            failure_class=REASON_MAP[reason],
            confidence=0.98,
            source="rules",
            rule_id="reason:" + reason,
            rationale="Structured gateway reason code '{}' maps to a single root cause.".format(reason),
        )

    text = (clean_description or payment.error_description or "").lower()
    if not text:
        return None

    for pattern, failure_class, rule_id in _COMPILED_TEXT:
        if pattern.search(text):
            return Diagnosis(
                payment_id=payment.payment_id,
                failure_class=failure_class,
                confidence=0.90,
                source="rules",
                rule_id=rule_id,
                rationale="Matched high-precision anchor '{}' in the gateway description.".format(rule_id),
            )

    return None


def is_known_ambiguous(payment: FailedPayment) -> bool:
    """Whether this row is one we expect to need a model. Used in reporting only."""
    text = (payment.error_description or "").lower()
    return any(marker in text for marker in KNOWN_AMBIGUOUS)
