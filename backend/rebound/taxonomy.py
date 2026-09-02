"""Failure taxonomy for Indian payment rails.

This module is deliberately *data*, not intelligence. Every downstream component
(rules classifier, LLM classifier, policy engine, simulator) is constrained to
these enums, which is what makes the agent's output auditable: the LLM is never
allowed to invent a failure class or an action.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, NamedTuple


class FailureClass(str, Enum):
    """Root cause of a failed payment. Closed set - the LLM picks from this or abstains."""

    INSUFFICIENT_FUNDS = "insufficient_funds"
    BANK_DOWNTIME = "bank_downtime"
    AUTH_DROPOFF = "auth_dropoff"              # customer abandoned OTP / 3DS / UPI collect
    EXPIRED_INSTRUMENT = "expired_instrument"  # expired or replaced card
    INVALID_INSTRUMENT = "invalid_instrument"  # wrong VPA, closed account, bad card number
    LIMIT_EXCEEDED = "limit_exceeded"          # per-txn or daily cap on the issuer side
    RISK_DECLINE_ISSUER = "risk_decline_issuer"
    SUSPECTED_FRAUD = "suspected_fraud"
    MANDATE_INACTIVE = "mandate_inactive"      # subscription/emandate paused, revoked, expired
    TECHNICAL_ERROR = "technical_error"        # gateway/PSP transient fault
    CUSTOMER_CANCELLED = "customer_cancelled"  # explicit user abort
    UNKNOWN = "unknown"                        # never actioned aggressively; routes to review


class InterventionType(str, Enum):
    """The bounded set of things the agent is allowed to do. Nothing else is executable."""

    RETRY_NOW = "retry_now"
    RETRY_SCHEDULED = "retry_scheduled"
    SWITCH_RAIL = "switch_rail"                # re-offer on a different instrument/rail
    NUDGE_LINK = "nudge_link"                  # payment link + message on a consented channel
    REQUEST_REMANDATE = "request_remandate"
    ESCALATE_HUMAN = "escalate_human"
    SUPPRESS = "suppress"                      # deliberate no-op, always with a reason


class Channel(str, Enum):
    WHATSAPP = "whatsapp"
    SMS = "sms"
    EMAIL = "email"
    NONE = "none"


class Rail(str, Enum):
    UPI = "upi"
    CARD = "card"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMANDATE = "emandate"


class ClassProfile(NamedTuple):
    """Everything the policy engine needs to reason about a failure class.

    ``base_recovery`` is the probability a payment of this class is eventually
    recovered *if left completely alone* (customer retries on their own).
    ``retryable`` gates whether a machine retry is ever legal for this class -
    this is a hard constraint, not a weight, because retrying a fraud decline is
    a compliance problem and not merely a bad expected value.
    """

    base_recovery: float
    retryable: bool
    contactable: bool
    decay_hours: float          # how fast the opportunity goes cold
    typical_share: float        # share of a realistic failed-payment batch


# Recovery/mix priors below are order-of-magnitude estimates assembled from public
# payment-industry reporting, not from Razorpay data. They are inputs to a
# SIMULATOR, and every headline number this project reports is therefore a
# simulated result. See docs/METHODOLOGY.md - we do not claim these are ground truth.
PROFILES: Dict[FailureClass, ClassProfile] = {
    FailureClass.INSUFFICIENT_FUNDS: ClassProfile(0.18, True, True, 96.0, 0.20),
    FailureClass.BANK_DOWNTIME: ClassProfile(0.30, True, True, 12.0, 0.13),
    FailureClass.AUTH_DROPOFF: ClassProfile(0.22, False, True, 6.0, 0.24),
    FailureClass.EXPIRED_INSTRUMENT: ClassProfile(0.08, False, True, 240.0, 0.06),
    FailureClass.INVALID_INSTRUMENT: ClassProfile(0.10, False, True, 72.0, 0.07),
    FailureClass.LIMIT_EXCEEDED: ClassProfile(0.20, True, True, 48.0, 0.05),
    FailureClass.RISK_DECLINE_ISSUER: ClassProfile(0.06, False, True, 48.0, 0.05),
    FailureClass.SUSPECTED_FRAUD: ClassProfile(0.01, False, False, 1.0, 0.02),
    FailureClass.MANDATE_INACTIVE: ClassProfile(0.05, False, True, 336.0, 0.06),
    FailureClass.TECHNICAL_ERROR: ClassProfile(0.35, True, True, 3.0, 0.09),
    FailureClass.CUSTOMER_CANCELLED: ClassProfile(0.12, False, True, 24.0, 0.03),
    FailureClass.UNKNOWN: ClassProfile(0.10, False, False, 24.0, 0.00),
}

# Hard compliance rails. The policy engine asserts against these; a violation is a
# crash, not a warning, because "the agent quietly did something illegal" is the
# failure mode that actually ends a payments company.
NEVER_RETRY: FrozenSet[FailureClass] = frozenset({
    FailureClass.SUSPECTED_FRAUD,
    FailureClass.RISK_DECLINE_ISSUER,
    FailureClass.INVALID_INSTRUMENT,
    FailureClass.EXPIRED_INSTRUMENT,
    FailureClass.MANDATE_INACTIVE,
    FailureClass.CUSTOMER_CANCELLED,
    FailureClass.AUTH_DROPOFF,
    FailureClass.UNKNOWN,
})

NEVER_CONTACT: FrozenSet[FailureClass] = frozenset({
    FailureClass.SUSPECTED_FRAUD,
    FailureClass.UNKNOWN,
})


def profile(fc: FailureClass) -> ClassProfile:
    return PROFILES[fc]
