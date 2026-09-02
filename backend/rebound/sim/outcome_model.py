"""The simulated world: did the money actually come back?

**Read this before believing any number this project reports.**

The obvious way to build this is to reuse `policy/economics.py` as the ground
truth. That would be worthless. The agent maximises expected value under that
table, so if the same table also decides what really happens, the agent wins by
construction - it is graded by its own beliefs. That is exactly the circularity
that made the Phase 2 rule metrics meaningless, and it is much harder to spot here
because the number it produces is money rather than accuracy.

So the world disagrees with the agent, deliberately and in three ways:

1. **Parameter noise.** Every efficacy value is the agent's belief multiplied by a
   seeded lognormal shock (default sigma 0.35). The agent is quantitatively wrong
   about almost everything.
2. **Structural bias.** A few beliefs are wrong in a specific direction rather than
   randomly - the agent over-rates nudges for empty accounts and under-rates rail
   switching. Real miscalibration is biased, not symmetric.
3. **Effects the agent does not model at all.** Contact fatigue and churn. The
   agent has an `annoyance` term it invented; the world has an actual backlash
   curve, and they are not the same function.

What this can therefore support: *given a world where root cause, timing and
channel matter roughly as we believe, does a bounded expected-value policy beat
naive alternatives, and does it keep beating them when its beliefs are wrong?*

What it cannot support: any claim about real recovery rates at a real merchant.
Those priors came from public industry reporting, not from Razorpay data. The
sensitivity sweep in `evaluate_policy.py` exists because of this limitation, not
in spite of it.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from ..models import ActionCandidate, FailedPayment, Outcome
from ..policy import economics
from ..taxonomy import NEVER_RETRY, Channel, FailureClass, InterventionType, profile

# Cost the merchant eats for pushing a fraud-declined payment back through the
# rails: scheme fees, ops handling, and the expected value of a chargeback. This
# is what stops "retry everything" looking free in the comparison.
FRAUD_RETRY_PENALTY_PAISE = 60_000.0
DISALLOWED_RETRY_PENALTY_PAISE = 1_500.0

# Beliefs the agent holds that are wrong in a *direction*, not just noisily.
# Multiplier applied to the agent's number to get the world's number.
STRUCTURAL_BIAS: Dict[Tuple[FailureClass, InterventionType], float] = {
    # People without money do not find money because you messaged them.
    (FailureClass.INSUFFICIENT_FUNDS, InterventionType.NUDGE_LINK): 0.55,
    # Offering a different rail works better than the agent expects.
    (FailureClass.RISK_DECLINE_ISSUER, InterventionType.SWITCH_RAIL): 1.45,
    (FailureClass.LIMIT_EXCEEDED, InterventionType.SWITCH_RAIL): 1.30,
    # Waiting out an outage is slightly less reliable than assumed.
    (FailureClass.BANK_DOWNTIME, InterventionType.RETRY_SCHEDULED): 0.85,
    # Drop-off customers are more recoverable than the agent thinks.
    (FailureClass.AUTH_DROPOFF, InterventionType.NUDGE_LINK): 1.20,
}

# Contact fatigue: the world's backlash curve, which the agent does not know.
FATIGUE_DECAY = 0.72          # efficacy multiplier per prior contact this window
CHURN_THRESHOLD = 3           # contacts before churn risk starts
CHURN_COST_PAISE = 25_000.0   # modelled lifetime value lost when a customer churns
CHURN_PROB_PER_EXTRA = 0.18


@dataclass
class WorldState:
    """Per-customer memory the world keeps while a policy runs against it."""

    contacts: Dict[str, int] = field(default_factory=dict)
    churned: set = field(default_factory=set)

    def contact(self, customer_id: str) -> int:
        prior = self.contacts.get(customer_id, 0)
        self.contacts[customer_id] = prior + 1
        return prior


class TrueWorld:
    """Ground truth. Independent of what the agent believes."""

    def __init__(self, seed: int = 101, sigma: float = 0.35):
        self.seed = seed
        self.sigma = sigma
        self._efficacy: Dict[Tuple[FailureClass, InterventionType], float] = {}
        self._build()

    def _build(self) -> None:
        """Perturb every belief once, deterministically, at construction.

        The shock is **mean-preserving**: ``exp(N(-sigma^2/2, sigma))`` has an
        expectation of exactly 1, so raising sigma spreads the agent's errors
        without making the world systematically easier or harder.

        This matters more than it looks. The naive `exp(N(0, sigma))` has mean
        `exp(sigma^2/2)`, so cranking sigma to 0.8 quietly makes every action 38%
        more effective - and the sensitivity sweep then measures "does blanket
        messaging win in an easier world" instead of "does the agent survive being
        wrong". The first version of this file had that bug and it inverted the
        conclusion at high sigma. See POSTMORTEM entry 5.
        """
        rng = random.Random(self.seed)
        drift = -0.5 * self.sigma * self.sigma
        for key, believed in economics.ACTION_EFFICACY.items():
            shock = math.exp(rng.gauss(drift, self.sigma))
            biased = believed * STRUCTURAL_BIAS.get(key, 1.0) * shock
            self._efficacy[key] = max(0.0, min(0.95, biased))

    def efficacy(self, failure_class: FailureClass, intervention: InterventionType) -> float:
        return self._efficacy.get((failure_class, intervention), 0.0)

    def simulate(
        self,
        payment: FailedPayment,
        true_class: FailureClass,
        action: ActionCandidate,
        state: WorldState,
        rng: random.Random,
    ) -> Outcome:
        """Play one payment forward under one action and return what happened.

        Note the action is scored against the payment's **true** failure class, not
        the agent's guess. A misclassification therefore costs real money here: the
        agent picks an intervention suited to a cause the payment does not have.
        """
        intervention = action.intervention
        cost = 0.0
        contacts = 0

        # Draw both uniforms up front, unconditionally. Every policy therefore
        # consumes exactly the same random numbers for a given payment, so the
        # comparison between policies is paired rather than confounded by luck.
        # (Common random numbers - it cuts the variance of the difference a lot.)
        u_recover = rng.random()
        u_churn = rng.random()

        # Organic recovery: some customers just try again themselves.
        p_organic = profile(true_class).base_recovery

        p_action = 0.0
        if intervention not in (InterventionType.SUPPRESS, InterventionType.ESCALATE_HUMAN):
            cost += economics.action_cost_paise(intervention, action.channel)

            is_retry = intervention in (
                InterventionType.RETRY_NOW, InterventionType.RETRY_SCHEDULED
            )
            if is_retry and true_class in NEVER_RETRY:
                # Retrying something that should never be retried does not work and
                # is not free. This is where naive baselines pay for their naivety.
                p_action = 0.0
                cost += (
                    FRAUD_RETRY_PENALTY_PAISE
                    if true_class is FailureClass.SUSPECTED_FRAUD
                    else DISALLOWED_RETRY_PENALTY_PAISE
                )
            else:
                p_action = self.efficacy(true_class, intervention)
                p_action *= economics.timing_multiplier(action.delay_hours, true_class)
                p_action *= economics.context_multiplier(payment)
                p_action *= economics.CHANNEL_EFFICACY.get(action.channel, 1.0)

            if action.channel is not Channel.NONE:
                prior = state.contact(payment.customer_id)
                contacts = 1
                # Fatigue the agent does not model: each earlier message makes the
                # next one land less well.
                p_action *= FATIGUE_DECAY ** prior
                if prior >= CHURN_THRESHOLD and payment.customer_id not in state.churned:
                    if u_churn < CHURN_PROB_PER_EXTRA:
                        state.churned.add(payment.customer_id)
                        cost += CHURN_COST_PAISE
                        p_action = 0.0

        p_action = max(0.0, min(0.95, p_action))
        # Two independent chances at the same money.
        p_total = 1.0 - (1.0 - p_organic) * (1.0 - p_action)

        recovered = u_recover < p_total
        hours = None
        if recovered:
            hours = max(0.1, action.delay_hours + rng.expovariate(1 / 6.0))

        return Outcome(
            payment_id=payment.payment_id,
            recovered=recovered,
            recovered_amount_paise=payment.amount if recovered else 0,
            hours_to_recovery=hours,
            customer_contacts=contacts,
            action_cost_paise=cost,
        )
