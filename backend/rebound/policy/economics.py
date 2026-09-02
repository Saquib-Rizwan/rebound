"""The expected-value model: is this intervention worth doing at all?

The agent does not pick actions because they sound helpful. For every candidate it
computes, in paise:

    EV  =  P(recover | class, action, timing, customer) * amount * margin
           - cash cost of the action
           - goodwill cost of contacting someone

and takes the best one, which is frequently ``SUPPRESS`` - an EV of exactly zero
beats any action whose expected return is negative. That is the whole reason the
agent stays quiet on fraud declines and tiny tickets instead of spraying messages.

Every number in this file is a stated assumption, not a fitted parameter. They are
grouped and commented so a reviewer can disagree with one value, change it, and
re-run the batch to see what it does to the money. Where a number came from a
plausible industry range rather than measurement, the comment says so.
"""
from __future__ import annotations

import math
from typing import Dict, Tuple

from .. import config
from ..models import FailedPayment
from ..taxonomy import Channel, FailureClass, InterventionType, Rail, profile

# P(recover) if this action is taken at its *ideal* moment, for this root cause.
# Read a row as: "given the payment failed for reason X, and we do Y at the best
# possible time, this is the chance the money eventually arrives."
ACTION_EFFICACY: Dict[Tuple[FailureClass, InterventionType], float] = {
    # Transient gateway faults are the easiest money in payments: the customer
    # already consented and nothing is actually wrong. Retry immediately.
    (FailureClass.TECHNICAL_ERROR, InterventionType.RETRY_NOW): 0.72,
    (FailureClass.TECHNICAL_ERROR, InterventionType.RETRY_SCHEDULED): 0.55,
    (FailureClass.TECHNICAL_ERROR, InterventionType.NUDGE_LINK): 0.30,

    # Downtime is the opposite: retrying *now* hits the same dead issuer. The
    # value is entirely in waiting for the recovery window.
    (FailureClass.BANK_DOWNTIME, InterventionType.RETRY_NOW): 0.14,
    (FailureClass.BANK_DOWNTIME, InterventionType.RETRY_SCHEDULED): 0.61,
    (FailureClass.BANK_DOWNTIME, InterventionType.SWITCH_RAIL): 0.44,
    (FailureClass.BANK_DOWNTIME, InterventionType.NUDGE_LINK): 0.26,

    # Balance arrives on a payday cycle, so timing dominates. An immediate retry
    # is close to worthless; a retry in two to three days is not.
    (FailureClass.INSUFFICIENT_FUNDS, InterventionType.RETRY_NOW): 0.06,
    (FailureClass.INSUFFICIENT_FUNDS, InterventionType.RETRY_SCHEDULED): 0.34,
    (FailureClass.INSUFFICIENT_FUNDS, InterventionType.NUDGE_LINK): 0.28,

    # The customer wanted to pay and got bored or distracted. A fast, friendly
    # link is the single highest-yield contact in the whole taxonomy.
    (FailureClass.AUTH_DROPOFF, InterventionType.NUDGE_LINK): 0.47,
    (FailureClass.AUTH_DROPOFF, InterventionType.SWITCH_RAIL): 0.29,

    # Nothing works until the customer supplies a different instrument.
    (FailureClass.EXPIRED_INSTRUMENT, InterventionType.NUDGE_LINK): 0.33,
    (FailureClass.EXPIRED_INSTRUMENT, InterventionType.SWITCH_RAIL): 0.21,
    (FailureClass.INVALID_INSTRUMENT, InterventionType.NUDGE_LINK): 0.24,
    (FailureClass.INVALID_INSTRUMENT, InterventionType.SWITCH_RAIL): 0.19,

    # A cap was hit. A different rail has a different cap.
    (FailureClass.LIMIT_EXCEEDED, InterventionType.SWITCH_RAIL): 0.41,
    (FailureClass.LIMIT_EXCEEDED, InterventionType.RETRY_SCHEDULED): 0.30,
    (FailureClass.LIMIT_EXCEEDED, InterventionType.NUDGE_LINK): 0.22,

    # The issuer said no on purpose. Hammering it is how merchants get their
    # decline rates flagged, so only a genuine rail change is on the table.
    (FailureClass.RISK_DECLINE_ISSUER, InterventionType.SWITCH_RAIL): 0.23,
    (FailureClass.RISK_DECLINE_ISSUER, InterventionType.NUDGE_LINK): 0.12,

    # Subscription plumbing: the authority itself has to be rebuilt.
    (FailureClass.MANDATE_INACTIVE, InterventionType.REQUEST_REMANDATE): 0.26,
    (FailureClass.MANDATE_INACTIVE, InterventionType.NUDGE_LINK): 0.15,

    # They chose to leave. One soft nudge is defensible; anything more is spam.
    (FailureClass.CUSTOMER_CANCELLED, InterventionType.NUDGE_LINK): 0.14,

    # Deliberately absent: every pairing for SUSPECTED_FRAUD and UNKNOWN. There is
    # no efficacy number that would make acting on those correct, so there is no
    # row to look up and the engine cannot construct the candidate.
}

# What a customer contact costs in goodwill, relative to the base annoyance, by
# how intrusive the channel is. WhatsApp interrupts; email mostly does not.
CHANNEL_INTRUSION: Dict[Channel, float] = {
    Channel.WHATSAPP: 1.0,
    Channel.SMS: 0.75,
    Channel.EMAIL: 0.25,
    Channel.NONE: 0.0,
}

# How well a nudge actually converts on each channel, relative to WhatsApp. This
# is the counterweight to CHANNEL_INTRUSION: email is cheap and unobtrusive but
# people ignore it, WhatsApp lands but costs goodwill. Without both terms the
# engine would send every message on the cheapest channel regardless of ticket.
CHANNEL_EFFICACY: Dict[Channel, float] = {
    Channel.WHATSAPP: 1.00,
    Channel.SMS: 0.78,
    Channel.EMAIL: 0.55,
    Channel.NONE: 1.00,
}

CHANNEL_CASH_COST: Dict[Channel, float] = {
    Channel.WHATSAPP: config.COST_PER_WHATSAPP_PAISE,
    Channel.SMS: config.COST_PER_SMS_PAISE,
    Channel.EMAIL: config.COST_PER_EMAIL_PAISE,
    Channel.NONE: 0.0,
}


def timing_multiplier(delay_hours: float, failure_class: FailureClass) -> float:
    """Opportunity decay. Waiting costs you attention, but sometimes buys success.

    Modelled as exponential decay on the class's own half-life, floored so that a
    late attempt is worth less rather than worth nothing.
    """
    decay = profile(failure_class).decay_hours
    if decay <= 0:
        return 1.0
    return max(0.25, math.exp(-delay_hours / (decay * 2.0)))


def context_multiplier(payment: FailedPayment) -> float:
    """Who this customer is changes how likely the recovery is.

    Two effects, both well attested in payments and both bounded so that no single
    signal can dominate the class-level prior:
      * a customer with a history of successful payments is a better bet
      * each failed attempt on the same payment lowers the odds of the next one
    """
    loyalty = 1.0 + min(0.30, 0.06 * payment.prior_success_count)
    fatigue = 0.72 ** max(0, payment.attempt_number - 1)
    return loyalty * fatigue


def p_recover(
    payment: FailedPayment,
    failure_class: FailureClass,
    intervention: InterventionType,
    delay_hours: float,
    channel: Channel = Channel.NONE,
) -> float:
    """Probability the money arrives if we take this action. Zero if disallowed."""
    if intervention in (InterventionType.SUPPRESS, InterventionType.ESCALATE_HUMAN):
        return 0.0

    base = ACTION_EFFICACY.get((failure_class, intervention))
    if base is None:
        return 0.0

    p = base * timing_multiplier(delay_hours, failure_class) * context_multiplier(payment)
    p *= CHANNEL_EFFICACY.get(channel, 1.0)
    return max(0.0, min(0.95, p))


def action_cost_paise(intervention: InterventionType, channel: Channel) -> float:
    """Cash we spend to attempt this, regardless of whether it works."""
    if intervention in (InterventionType.RETRY_NOW, InterventionType.RETRY_SCHEDULED):
        return config.COST_PER_RETRY_PAISE
    if intervention is InterventionType.ESCALATE_HUMAN:
        return config.COST_PER_HUMAN_REVIEW_PAISE
    if intervention is InterventionType.SUPPRESS:
        return 0.0
    # Everything else reaches the customer over some channel.
    return CHANNEL_CASH_COST.get(channel, 0.0) + config.COST_PER_RETRY_PAISE * 0.25


def annoyance_paise(intervention: InterventionType, channel: Channel) -> float:
    """The price we put on bothering someone.

    This is the term that makes the agent quiet. Without it, any contact with a
    non-zero success probability looks profitable and the optimal policy is to
    message everybody forever - which is how recovery tools get merchants blocked
    on WhatsApp and hated by customers.
    """
    if intervention in (
        InterventionType.RETRY_NOW,
        InterventionType.RETRY_SCHEDULED,
        InterventionType.SUPPRESS,
        InterventionType.ESCALATE_HUMAN,
    ):
        return 0.0
    return config.ANNOYANCE_PAISE * CHANNEL_INTRUSION.get(channel, 1.0)


def expected_value_paise(
    payment: FailedPayment,
    failure_class: FailureClass,
    intervention: InterventionType,
    delay_hours: float,
    channel: Channel,
) -> Tuple[float, float, float, float, float]:
    """Returns (ev, p, gross, cash_cost, annoyance) - all components, for the audit.

    Gross uses merchant margin, not ticket value: recovering a INR 1,000 order does
    not put INR 1,000 in the merchant's pocket, and deciding as if it did would
    justify spending far too much to chase it.
    """
    p = p_recover(payment, failure_class, intervention, delay_hours, channel)
    gross = p * payment.amount * config.MERCHANT_MARGIN
    cash = action_cost_paise(intervention, channel)
    annoy = annoyance_paise(intervention, channel)
    return gross - cash - annoy, p, gross, cash, annoy
