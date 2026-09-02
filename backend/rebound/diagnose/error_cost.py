"""What a misclassification actually costs, in rupees.

Accuracy treats every mistake as equal. Payments do not. Calling a fraud decline
"insufficient funds" sends the agent off to retry a transaction the issuer already
refused - you eat the fee, you may eat a chargeback, and you have a compliance
conversation. Calling insufficient funds "fraud" merely means you stay quiet and
lose a recovery you could have had. Those are not the same error and a single
accuracy figure hides the difference entirely.

So we price errors instead of counting them. The cost of predicting ``pred`` when
the truth is ``true`` is built from three components:

  compliance   acting on something that must never be acted on
  under-treat  choosing a weaker response than the truth warranted, losing uplift
  over-treat   spending money and goodwill on a response that cannot work

Every constant below is an explicit, arguable assumption rather than a fitted
parameter, and they are all in one place so a reviewer can disagree with a number
and re-run. This is a model of cost, not a measurement of it.
"""
from __future__ import annotations

from typing import Dict, Tuple

from .. import config
from ..taxonomy import NEVER_CONTACT, NEVER_RETRY, FailureClass, profile

# Fixed operational cost of putting a fraud-declined payment back through the
# rails: gateway fee, ops time, and the expected value of the chargeback risk.
FRAUD_MISROUTE_OPS_PAISE = 50_000.0          # INR 500
# Same idea, but for the merely-should-not-have-retried classes.
WRONG_RETRY_OPS_PAISE = 1_200.0              # INR 12
# Share of the ticket assumed lost when a fraudulent payment is pushed through.
FRAUD_LOSS_FRACTION = 1.0


def error_cost_paise(true_class: FailureClass, pred_class: FailureClass, amount_paise: int) -> float:
    """Rupee (paise) cost of one classification error. Zero when correct."""
    if true_class is pred_class:
        return 0.0

    cost = 0.0
    margin = config.MERCHANT_MARGIN

    # 1. Compliance and loss. Predicting a retryable class for something that must
    #    never be retried is the expensive direction, by a wide margin.
    if true_class in NEVER_RETRY and pred_class not in NEVER_RETRY:
        if true_class is FailureClass.SUSPECTED_FRAUD:
            cost += FRAUD_MISROUTE_OPS_PAISE + amount_paise * FRAUD_LOSS_FRACTION
        else:
            cost += WRONG_RETRY_OPS_PAISE

    # 2. Unwanted contact: the truth said stay silent, the prediction says speak.
    if true_class in NEVER_CONTACT and pred_class not in NEVER_CONTACT:
        cost += config.ANNOYANCE_PAISE + config.COST_PER_WHATSAPP_PAISE

    # 3. Under- or over-treatment, priced off how recoverable each class is.
    true_recoverable = profile(true_class).base_recovery
    pred_recoverable = profile(pred_class).base_recovery
    gap = true_recoverable - pred_recoverable

    if gap > 0:
        # We will treat this too gently and forgo margin we could have recovered.
        cost += gap * amount_paise * margin
    else:
        # We will spend on an intervention with less chance of landing than we think.
        cost += config.COST_PER_RETRY_PAISE + config.ANNOYANCE_PAISE * 0.5

    return cost


def cost_matrix(amount_paise: int = 100_000) -> Dict[Tuple[str, str], float]:
    """Full matrix at a reference ticket size, for the report table."""
    classes = list(FailureClass)
    return {
        (t.value, p.value): error_cost_paise(t, p, amount_paise)
        for t in classes
        for p in classes
    }


def worst_confusions(amount_paise: int = 100_000, top_n: int = 6):
    """The confusions that would hurt most. Drives what we test hardest."""
    matrix = cost_matrix(amount_paise)
    ranked = sorted(matrix.items(), key=lambda kv: kv[1], reverse=True)
    return [(t, p, c) for (t, p), c in ranked[:top_n]]
