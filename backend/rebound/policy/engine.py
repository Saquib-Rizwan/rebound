"""The decision engine: propose, price, filter, choose - and usually choose silence.

For each failed payment the engine:

  1. proposes every action the root cause makes sensible (the playbook)
  2. prices each one with the expected-value model
  3. asks the guardrails whether it is permitted at all
  4. picks the highest expected value among the permitted, with SUPPRESS at zero
     always on the table

Because SUPPRESS scores exactly zero, any action whose expected return is negative
loses to doing nothing. That is not a special case bolted on afterwards - it falls
out of the arithmetic, which is why the agent stays quiet without being told to.

Rejected candidates are kept on the Decision with the guardrail that stopped them.
An audit trail that only records what happened is much less useful than one that
records what was considered.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .. import config
from ..models import ActionCandidate, Decision, Diagnosis, FailedPayment
from ..taxonomy import Channel, FailureClass, InterventionType, Rail
from . import economics, guardrails
from .guardrails import CONTACT_ACTIONS, AgentState

# When to try again, per root cause. These encode the domain claim that *timing is
# the intervention* - the same retry is worthless at minute one and valuable on
# day two, and vice versa.
RETRY_DELAYS: Dict[FailureClass, List[float]] = {
    FailureClass.TECHNICAL_ERROR: [0.0, 0.5, 2.0],
    FailureClass.BANK_DOWNTIME: [1.0, 3.0, 8.0],          # ride out the outage
    FailureClass.INSUFFICIENT_FUNDS: [24.0, 48.0, 72.0],  # wait for the salary cycle
    FailureClass.LIMIT_EXCEEDED: [24.0, 48.0],            # daily caps reset
}

# Which alternative rail to offer when the current one is the problem.
RAIL_SWITCH: Dict[Rail, Rail] = {
    Rail.CARD: Rail.UPI,
    Rail.UPI: Rail.CARD,
    Rail.NETBANKING: Rail.UPI,
    Rail.WALLET: Rail.UPI,
    Rail.EMANDATE: Rail.UPI,
}

CHANNEL_PREFERENCE = [Channel.WHATSAPP, Channel.SMS, Channel.EMAIL]

# Above this, an unexplained failure is worth a human's time rather than silence.
ESCALATION_TICKET_PAISE = int(config.DAILY_ACTION_BUDGET_PAISE * 0.5)


def _hours_until_quiet_end(now: datetime) -> float:
    """How long until messaging is allowed again. Zero if it already is."""
    start, end = config.QUIET_HOURS_START, config.QUIET_HOURS_END
    hour = now.hour + now.minute / 60.0
    in_quiet = hour >= start or hour < end if start > end else start <= hour < end
    if not in_quiet:
        return 0.0
    return (end - hour) % 24.0


def _price(
    payment: FailedPayment,
    failure_class: FailureClass,
    intervention: InterventionType,
    delay_hours: float,
    channel: Channel,
    target_rail: Optional[Rail] = None,
    calibrator=None,
) -> ActionCandidate:
    ev, p, gross, cash, annoy = economics.expected_value_paise(
        payment, failure_class, intervention, delay_hours, channel, calibrator
    )
    return ActionCandidate(
        intervention=intervention,
        delay_hours=delay_hours,
        channel=channel,
        target_rail=target_rail,
        p_recover=round(p, 4),
        gross_value_paise=round(gross, 2),
        cost_paise=round(cash, 2),
        annoyance_paise=round(annoy, 2),
        expected_value_paise=round(ev, 2),
    )


def propose(payment: FailedPayment, diagnosis: Diagnosis, now: datetime,
            calibrator=None) -> List[ActionCandidate]:
    """Every action worth pricing for this payment. Illegal ones are not proposed."""
    failure_class = diagnosis.failure_class

    def price(*args, **kwargs):
        return _price(*args, calibrator=calibrator, **kwargs)

    candidates: List[ActionCandidate] = [
        # Always available, always zero. This is the bar every other action must clear.
        price(payment, failure_class, InterventionType.SUPPRESS, 0.0, Channel.NONE)
    ]

    if failure_class in (FailureClass.UNKNOWN, FailureClass.SUSPECTED_FRAUD):
        candidates.append(
            price(payment, failure_class, InterventionType.ESCALATE_HUMAN, 0.0, Channel.NONE)
        )
        return candidates

    retry_delays = list(RETRY_DELAYS.get(failure_class, []))
    if payment.is_recurring or payment.rail is Rail.EMANDATE:
        # A recurring debit cannot lawfully run before its pre-debit notification.
        # Offer compliant windows rather than proposing options the rails will
        # simply reject - the agent should know the rules, not discover them.
        notice = config.PRE_DEBIT_NOTICE_HOURS
        retry_delays = sorted({max(d, notice) for d in retry_delays} | {notice, notice + 24.0})

    for delay in retry_delays:
        intervention = (
            InterventionType.RETRY_NOW if delay == 0.0 else InterventionType.RETRY_SCHEDULED
        )
        candidates.append(price(payment, failure_class, intervention, delay, Channel.NONE))

    # Contact actions, on every channel the customer has consented to. Where we are
    # currently inside quiet hours, also propose the same message timed to land the
    # moment quiet hours end - so the guardrail redirects the action instead of
    # simply killing it.
    quiet_delay = _hours_until_quiet_end(now)
    contact_delays = [0.0] if quiet_delay == 0.0 else [0.0, quiet_delay]

    for channel in CHANNEL_PREFERENCE:
        if not payment.contact_consent.get(channel.value, False):
            continue
        for delay in contact_delays:
            candidates.append(
                price(payment, failure_class, InterventionType.NUDGE_LINK, delay, channel)
            )
            if failure_class is FailureClass.MANDATE_INACTIVE:
                candidates.append(
                    price(
                        payment, failure_class, InterventionType.REQUEST_REMANDATE, delay, channel
                    )
                )
            else:
                candidates.append(
                    price(
                        payment,
                        failure_class,
                        InterventionType.SWITCH_RAIL,
                        delay,
                        channel,
                        RAIL_SWITCH.get(payment.rail),
                    )
                )

    return candidates


def decide(
    payment: FailedPayment,
    diagnosis: Diagnosis,
    state: AgentState,
    now: datetime,
    calibrator=None,
) -> Decision:
    """Score, filter, and choose. Returns a fully auditable Decision.

    ``calibrator`` is optional. Without it the agent uses the hand-written
    efficacy priors and behaves deterministically. With it, efficacy comes from
    posteriors fitted to observed outcomes, and small-ticket decisions explore.
    """
    candidates = propose(payment, diagnosis, now, calibrator)

    permitted: List[ActionCandidate] = []
    applied: List[str] = []
    for candidate in candidates:
        blocked = guardrails.check(payment, diagnosis, candidate, state, now)
        if blocked:
            candidate.blocked_by = blocked
            if blocked not in applied:
                applied.append(blocked)
        else:
            permitted.append(candidate)

    suppress = next(
        c for c in candidates if c.intervention is InterventionType.SUPPRESS
    )
    chosen = max(permitted, key=lambda c: c.expected_value_paise) if permitted else suppress

    # A high-value payment we cannot explain is worth a person looking at it, even
    # though escalation never wins on expected value - a human review costs real
    # money and returns none directly. This is a deliberate override of the
    # arithmetic, and it is recorded as one.
    if (
        diagnosis.failure_class is FailureClass.UNKNOWN
        and payment.amount >= ESCALATION_TICKET_PAISE
        and not state.killed
    ):
        escalation = next(
            (c for c in permitted if c.intervention is InterventionType.ESCALATE_HUMAN), None
        )
        if escalation is not None:
            chosen = escalation
            applied.append("OVR_high_value_unknown")

    if chosen.expected_value_paise <= 0 and chosen.intervention not in (
        InterventionType.SUPPRESS,
        InterventionType.ESCALATE_HUMAN,
    ):
        chosen = suppress

    return Decision(
        payment_id=payment.payment_id,
        merchant_id=payment.merchant_id,
        customer_id=payment.customer_id,
        amount_paise=payment.amount,
        diagnosis=diagnosis,
        chosen=chosen,
        considered=candidates,
        guardrails_applied=applied,
        policy_version=config.POLICY_VERSION,
        decided_at=now,
        explanation=explain(payment, diagnosis, chosen, candidates),
    )


def explain(
    payment: FailedPayment,
    diagnosis: Diagnosis,
    chosen: ActionCandidate,
    considered: List[ActionCandidate],
) -> str:
    """Plain-language reason, including why the runner-up lost.

    Written for a merchant support agent reading one row in a dashboard, not for
    an engineer reading a log.
    """
    rupees = payment.amount / 100.0

    if chosen.intervention is InterventionType.SUPPRESS:
        blocked = [c for c in considered if c.blocked_by]
        if blocked:
            reason = guardrails.DESCRIPTIONS.get(blocked[0].blocked_by, blocked[0].blocked_by)
            return (
                "Doing nothing on INR {:,.0f} ({}). Best available action was blocked: {}.".format(
                    rupees, diagnosis.failure_class.value, reason.lower()
                )
            )
        if diagnosis.failure_class is FailureClass.SUSPECTED_FRAUD:
            return (
                "Doing nothing on INR {:,.0f}. This was declined as suspected fraud, so "
                "no automated retry or customer contact is permitted at any value - "
                "pushing it back through the rails is what costs merchants chargebacks."
            ).format(rupees)

        best_negative = max(
            (c for c in considered if c.intervention is not InterventionType.SUPPRESS),
            key=lambda c: c.expected_value_paise,
            default=None,
        )
        if best_negative is not None:
            return (
                "Doing nothing on INR {:,.0f} ({}). The best action, {}, was worth "
                "INR {:,.0f} in expected margin against INR {:,.0f} of cost and goodwill - "
                "not worth doing.".format(
                    rupees, diagnosis.failure_class.value, best_negative.intervention.value,
                    best_negative.gross_value_paise / 100,
                    (best_negative.cost_paise + best_negative.annoyance_paise) / 100,
                )
            )
        return "Doing nothing on INR {:,.0f}: no action is permitted for {}.".format(
            rupees, diagnosis.failure_class.value
        )

    if chosen.intervention is InterventionType.ESCALATE_HUMAN:
        return (
            "Sending INR {:,.0f} to a human. Root cause could not be established "
            "({}), and the ticket is large enough that silence is the wrong default.".format(
                rupees, diagnosis.rationale[:80] or "no confident classification"
            )
        )

    verb = {
        InterventionType.RETRY_NOW: "Retrying immediately",
        InterventionType.RETRY_SCHEDULED: "Retrying",
        InterventionType.NUDGE_LINK: "Sending a payment link",
        InterventionType.SWITCH_RAIL: "Offering an alternative payment method",
        InterventionType.REQUEST_REMANDATE: "Asking to re-authorise the mandate",
    }.get(chosen.intervention, chosen.intervention.value.replace("_", " ").capitalize())

    if chosen.intervention is InterventionType.RETRY_NOW or chosen.delay_hours == 0:
        timing = ""
    else:
        timing = " in {:.0f}h".format(chosen.delay_hours)
    via = "" if chosen.channel is Channel.NONE else " via {}".format(chosen.channel.value)
    return (
        "{}{}{} on INR {:,.0f}. Cause: {} ({:.0%} confidence, {}). "
        "Estimated {:.0%} chance of recovery, worth INR {:,.0f} in expected margin "
        "against INR {:,.0f} of cost.".format(
            verb, timing, via, rupees,
            diagnosis.failure_class.value, diagnosis.confidence, diagnosis.source,
            chosen.p_recover, chosen.gross_value_paise / 100,
            (chosen.cost_paise + chosen.annoyance_paise) / 100,
        )
    )


def apply_to_state(decision: Decision, state: AgentState) -> None:
    """Commit the side effects a decision has on future decisions."""
    chosen = decision.chosen
    if chosen.intervention is InterventionType.SUPPRESS:
        return

    when = decision.decided_at + timedelta(hours=chosen.delay_hours)
    state.attempts_by_payment[decision.payment_id] = (
        state.attempts_by_payment.get(decision.payment_id, 0) + 1
    )
    state.record_spend(decision.merchant_id, decision.decided_at, chosen.cost_paise)
    if chosen.intervention in CONTACT_ACTIONS:
        state.record_contact(decision.customer_id, when)
