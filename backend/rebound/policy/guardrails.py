"""Hard limits. These run before economics and cannot be outvoted by expected value.

The distinction matters. The EV model in ``economics.py`` answers "is this worth
doing"; this module answers "are we allowed to do this at all". Keeping them
separate means a guardrail can never be traded away because the money looked good,
which is exactly the failure mode that gets a payments product switched off.

Every guardrail has a stable id. Blocked candidates are kept on the ``Decision``
with the id that stopped them, so the audit trail shows what the agent *wanted* to
do as well as what it did.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from .. import config
from ..models import ActionCandidate, Diagnosis, FailedPayment
from ..taxonomy import (NEVER_CONTACT, NEVER_RETRY, Channel, FailureClass,
                        InterventionType, Rail)

CONTACT_ACTIONS = frozenset({
    InterventionType.NUDGE_LINK,
    InterventionType.SWITCH_RAIL,
    InterventionType.REQUEST_REMANDATE,
})
RETRY_ACTIONS = frozenset({
    InterventionType.RETRY_NOW,
    InterventionType.RETRY_SCHEDULED,
})


@dataclass
class AgentState:
    """Everything the guardrails need to remember between decisions.

    Kept explicit and in one place rather than scattered through the engine, so
    that a stopping rule can be reasoned about without reading the whole agent.
    """

    contacts_by_customer: Dict[str, List[datetime]] = field(default_factory=dict)
    spend_by_merchant_day: Dict[Tuple[str, date], float] = field(default_factory=dict)
    attempts_by_payment: Dict[str, int] = field(default_factory=dict)
    killed: bool = False
    kill_reason: str = ""

    def record_contact(self, customer_id: str, when: datetime) -> None:
        self.contacts_by_customer.setdefault(customer_id, []).append(when)

    def record_spend(self, merchant_id: str, when: datetime, paise: float) -> None:
        key = (merchant_id, when.date())
        self.spend_by_merchant_day[key] = self.spend_by_merchant_day.get(key, 0.0) + paise

    def spend_today(self, merchant_id: str, when: datetime) -> float:
        return self.spend_by_merchant_day.get((merchant_id, when.date()), 0.0)

    def contacts_since(self, customer_id: str, since: datetime) -> List[datetime]:
        return [t for t in self.contacts_by_customer.get(customer_id, []) if t >= since]

    def last_contact(self, customer_id: str) -> Optional[datetime]:
        history = self.contacts_by_customer.get(customer_id)
        return max(history) if history else None

    def kill(self, reason: str) -> None:
        """Global stop. Nothing executes until a human clears it."""
        self.killed = True
        self.kill_reason = reason


# A guardrail returns a blocking id, or None to allow.
Guardrail = Callable[[FailedPayment, Diagnosis, ActionCandidate, AgentState, datetime], Optional[str]]


def g_kill_switch(payment, diagnosis, candidate, state, now):
    """One switch that stops every outbound action, for when something is wrong."""
    if state.killed and candidate.intervention is not InterventionType.SUPPRESS:
        return "G00_kill_switch"
    return None


def g_never_retry_class(payment, diagnosis, candidate, state, now):
    """Some root causes must never be machine-retried. Fraud is the obvious one.

    This is a compliance rail, not an optimisation. A retry against a fraud
    decline costs the merchant a fee, risks a chargeback, and is the sort of thing
    that shows up in an issuer's decline-rate review.
    """
    if candidate.intervention in RETRY_ACTIONS and diagnosis.failure_class in NEVER_RETRY:
        return "G01_never_retry_class"
    return None


def g_never_contact_class(payment, diagnosis, candidate, state, now):
    """Never message someone about a payment we flagged as fraud or cannot explain."""
    if candidate.intervention in CONTACT_ACTIONS and diagnosis.failure_class in NEVER_CONTACT:
        return "G02_never_contact_class"
    return None


def g_max_attempts(payment, diagnosis, candidate, state, now):
    """Stopping rule: a bounded number of tries per payment, then we stop forever."""
    if candidate.intervention is InterventionType.SUPPRESS:
        return None
    attempts = state.attempts_by_payment.get(payment.payment_id, payment.attempt_number - 1)
    if attempts >= config.MAX_ATTEMPTS_PER_PAYMENT:
        return "G03_max_attempts"
    return None


def g_channel_consent(payment, diagnosis, candidate, state, now):
    """No consent, no message. Checked per channel, not per customer."""
    if candidate.intervention not in CONTACT_ACTIONS:
        return None
    if candidate.channel is Channel.NONE:
        return "G04_no_channel"
    if not payment.contact_consent.get(candidate.channel.value, False):
        return "G04_channel_consent"
    return None


def g_quiet_hours(payment, diagnosis, candidate, state, now):
    """No outbound messages overnight. A retry is silent, so retries are exempt."""
    if candidate.intervention not in CONTACT_ACTIONS:
        return None
    send_at = now + timedelta(hours=candidate.delay_hours)
    hour = send_at.hour
    start, end = config.QUIET_HOURS_START, config.QUIET_HOURS_END
    in_quiet = hour >= start or hour < end if start > end else start <= hour < end
    return "G05_quiet_hours" if in_quiet else None


def g_contact_cooldown(payment, diagnosis, candidate, state, now):
    """One conversation at a time. Do not stack messages on the same person."""
    if candidate.intervention not in CONTACT_ACTIONS:
        return None
    last = state.last_contact(payment.customer_id)
    if last and (now - last) < timedelta(hours=config.CONTACT_COOLDOWN_HOURS):
        return "G06_contact_cooldown"
    return None


def g_contact_frequency_cap(payment, diagnosis, candidate, state, now):
    """A weekly ceiling per customer across all their failed payments."""
    if candidate.intervention not in CONTACT_ACTIONS:
        return None
    recent = state.contacts_since(payment.customer_id, now - timedelta(days=7))
    if len(recent) >= config.MAX_CONTACTS_PER_CUSTOMER_PER_WEEK:
        return "G07_frequency_cap"
    return None


def g_min_ticket(payment, diagnosis, candidate, state, now):
    """Do not spend a message on a ticket too small to be worth the interruption."""
    if candidate.intervention in CONTACT_ACTIONS and payment.amount < config.MIN_TICKET_TO_CONTACT_PAISE:
        return "G08_min_ticket"
    return None


def g_daily_budget(payment, diagnosis, candidate, state, now):
    """Per-merchant daily spend cap on recovery actions. Trips loudly, not silently."""
    if candidate.intervention is InterventionType.SUPPRESS:
        return None
    projected = state.spend_today(payment.merchant_id, now) + candidate.cost_paise
    if projected > config.DAILY_ACTION_BUDGET_PAISE:
        return "G09_daily_budget"
    return None


def g_unknown_class(payment, diagnosis, candidate, state, now):
    """If we do not know why it failed, we do not act on it.

    This is what makes the classifier's confidence floor and injection demotion
    meaningful: both route to UNKNOWN, and UNKNOWN can only be suppressed or sent
    to a human. Uncertainty upstream becomes inaction downstream, by construction.
    """
    if diagnosis.failure_class is not FailureClass.UNKNOWN:
        return None
    if candidate.intervention in (InterventionType.SUPPRESS, InterventionType.ESCALATE_HUMAN):
        return None
    return "G10_unknown_class"


def _is_recurring(payment: FailedPayment) -> bool:
    return payment.is_recurring or payment.rail is Rail.EMANDATE


def _afa_ceiling(payment: FailedPayment) -> int:
    """Above this, RBI requires additional factor authentication again."""
    if payment.merchant_id in config.AFA_EXEMPT_MERCHANTS:
        return config.AFA_THRESHOLD_HIGH_PAISE
    return config.AFA_THRESHOLD_PAISE


def g_emandate_pre_debit_notice(payment, diagnosis, candidate, state, now):
    """RBI: a recurring debit needs a pre-debit notification 24 hours ahead.

    So an e-mandate retry cannot be immediate, however good the expected value
    looks. The agent may still retry - it simply has to schedule far enough out
    that the mandatory notification can precede the debit. This is why the policy
    proposes long-delay retries for recurring payments at all.
    """
    if candidate.intervention not in RETRY_ACTIONS:
        return None
    if not _is_recurring(payment):
        return None
    if candidate.delay_hours < config.PRE_DEBIT_NOTICE_HOURS:
        return "G11_pre_debit_notice"
    return None


def g_emandate_afa_threshold(payment, diagnosis, candidate, state, now):
    """RBI: recurring debits above the AFA threshold need authentication again.

    A silent machine retry cannot satisfy that - the customer has to be present.
    So above the ceiling the only lawful recovery paths are ones that put the
    customer back in an authenticated flow: a payment link, or re-mandating.
    """
    if candidate.intervention not in RETRY_ACTIONS:
        return None
    if not _is_recurring(payment):
        return None
    if payment.amount > _afa_ceiling(payment):
        return "G12_afa_required"
    return None


ALL_GUARDRAILS: List[Guardrail] = [
    g_kill_switch,
    g_never_retry_class,
    g_never_contact_class,
    g_unknown_class,
    g_max_attempts,
    g_emandate_pre_debit_notice,
    g_emandate_afa_threshold,
    g_channel_consent,
    g_quiet_hours,
    g_contact_cooldown,
    g_contact_frequency_cap,
    g_min_ticket,
    g_daily_budget,
]

DESCRIPTIONS: Dict[str, str] = {
    "G00_kill_switch": "Global kill switch is engaged",
    "G01_never_retry_class": "This root cause must never be machine-retried",
    "G02_never_contact_class": "This root cause must never trigger a customer contact",
    "G03_max_attempts": "Attempt ceiling for this payment reached",
    "G04_no_channel": "No channel available for this contact",
    "G04_channel_consent": "Customer has not consented on this channel",
    "G05_quiet_hours": "Send time falls inside quiet hours",
    "G06_contact_cooldown": "Customer contacted too recently",
    "G07_frequency_cap": "Customer weekly contact cap reached",
    "G08_min_ticket": "Ticket too small to justify contacting the customer",
    "G11_pre_debit_notice":
        "RBI e-mandate rules require a pre-debit notification 24 hours before a "
        "recurring debit, so this retry cannot run any sooner",
    "G12_afa_required":
        "RBI e-mandate rules require additional factor authentication above the "
        "threshold, so this recurring debit cannot be retried silently",
    "G09_daily_budget": "Merchant daily action budget would be exceeded",
    "G10_unknown_class": "Root cause unknown; only suppress or escalate is permitted",
}


def check(
    payment: FailedPayment,
    diagnosis: Diagnosis,
    candidate: ActionCandidate,
    state: AgentState,
    now: datetime,
) -> Optional[str]:
    """First blocking guardrail id, or None if the candidate is permitted."""
    for guardrail in ALL_GUARDRAILS:
        blocked = guardrail(payment, diagnosis, candidate, state, now)
        if blocked:
            return blocked
    return None
