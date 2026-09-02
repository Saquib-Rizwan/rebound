"""The alternatives Rebound has to beat.

A recovery agent with no comparison group is a demo. These are the four things a
merchant would plausibly do instead, including the two that most recovery tools
actually do:

  do_nothing    the honest floor. Some customers retry by themselves and the
                merchant spends nothing. Any agent that cannot beat this is
                worse than useless.
  retry_all     retry every failure immediately, up to the attempt ceiling.
                Cheap, obvious, and quietly expensive - it retries fraud
                declines and dead cards too.
  blind_24h     retry everything once, tomorrow. The classic cron job.
  nudge_all     message every customer straight away on the best channel they
                have consented to. What a growth team ships in a week.
  rebound       the agent: classify, price, filter, choose.

Baselines deliberately do **not** see the classifier output. That is the whole
point of the comparison - they are naive because naivety is what they are
modelling. They do respect consent, because ignoring consent is not a baseline,
it is a lawsuit.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, List, Optional

from .. import config
from ..models import ActionCandidate, Decision, Diagnosis, FailedPayment
from ..policy import engine
from ..policy.guardrails import AgentState
from ..taxonomy import Channel, FailureClass, InterventionType

CHANNEL_ORDER = [Channel.WHATSAPP, Channel.SMS, Channel.EMAIL]


def _consented_channel(payment: FailedPayment) -> Optional[Channel]:
    for channel in CHANNEL_ORDER:
        if payment.contact_consent.get(channel.value, False):
            return channel
    return None


def _wrap(
    payment: FailedPayment,
    diagnosis: Diagnosis,
    candidate: ActionCandidate,
    now: datetime,
    policy_name: str,
) -> Decision:
    return Decision(
        payment_id=payment.payment_id,
        merchant_id=payment.merchant_id,
        customer_id=payment.customer_id,
        amount_paise=payment.amount,
        diagnosis=diagnosis,
        chosen=candidate,
        considered=[candidate],
        guardrails_applied=[],
        policy_version=policy_name,
        decided_at=now,
        explanation="baseline policy: " + policy_name,
    )


def _action(
    intervention: InterventionType,
    delay_hours: float = 0.0,
    channel: Channel = Channel.NONE,
) -> ActionCandidate:
    return ActionCandidate(
        intervention=intervention, delay_hours=delay_hours, channel=channel
    )


def do_nothing(payment, diagnosis, state, now):
    return _wrap(payment, diagnosis, _action(InterventionType.SUPPRESS), now, "do_nothing")


def retry_all(payment, diagnosis, state, now):
    attempts = state.attempts_by_payment.get(payment.payment_id, 0)
    if attempts >= config.MAX_ATTEMPTS_PER_PAYMENT:
        return _wrap(payment, diagnosis, _action(InterventionType.SUPPRESS), now, "retry_all")
    state.attempts_by_payment[payment.payment_id] = attempts + 1
    return _wrap(payment, diagnosis, _action(InterventionType.RETRY_NOW), now, "retry_all")


def blind_24h(payment, diagnosis, state, now):
    return _wrap(
        payment, diagnosis,
        _action(InterventionType.RETRY_SCHEDULED, delay_hours=24.0),
        now, "blind_24h",
    )


def nudge_all(payment, diagnosis, state, now):
    channel = _consented_channel(payment)
    if channel is None:
        return _wrap(payment, diagnosis, _action(InterventionType.SUPPRESS), now, "nudge_all")
    return _wrap(
        payment, diagnosis,
        _action(InterventionType.NUDGE_LINK, channel=channel),
        now, "nudge_all",
    )


def rebound(payment, diagnosis, state, now):
    """The real agent. The only policy that reads the diagnosis."""
    decision = engine.decide(payment, diagnosis, state, now)
    engine.apply_to_state(decision, state)
    return decision


POLICIES = {
    "do_nothing": do_nothing,
    "retry_all": retry_all,
    "blind_24h": blind_24h,
    "nudge_all": nudge_all,
    "rebound": rebound,
}

# Order used in reports: floor first, then increasingly sophisticated naive
# approaches, then the agent.
POLICY_ORDER = ["do_nothing", "retry_all", "blind_24h", "nudge_all", "rebound"]
