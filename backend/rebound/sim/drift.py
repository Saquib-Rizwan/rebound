"""Held-out drift set: does the classifier work on text it was not designed against?

The honest weakness of the main batch is that the same author wrote both the
generator templates and the regex anchors, so 100% rule precision there proves
less than it looks. Production traffic does not come from our own templates - new
issuers appear, PSPs reword their messages, and switches truncate strings.

So this module builds a second test set the rules were never tuned on:

  paraphrase  a held-out vocabulary. Same 12 root causes, deliberately worded to
              avoid every anchor phrase in rules.py. This is the fair test.
  noise       character-level corruption of the original strings: typos, dropped
              words, upper-casing, truncation. This is the robustness test.

Expect rule coverage to fall here. That fall is the measured argument for keeping
a model in the pipeline at all - if rules held up perfectly on unseen wording, the
honest engineering answer would be to delete the model.
"""
from __future__ import annotations

import random
from typing import Dict, List, Tuple

from ..models import FailedPayment
from ..taxonomy import FailureClass

# Held-out phrasings. Written to describe the same root cause using none of the
# anchor words in rules.py - no "insufficient", no "RC 91", no "abandoned".
PARAPHRASE: Dict[FailureClass, List[str]] = {
    FailureClass.INSUFFICIENT_FUNDS: [
        "payer wallet could not cover the ticket value",
        "debit refused, available limit under the requested sum",
        "shortfall in the source account at the time of capture",
    ],
    FailureClass.BANK_DOWNTIME: [
        "remitter switch offline during the attempt window",
        "no reply from the issuing host after three probes",
        "core banking upgrade in progress at the payer institution",
    ],
    FailureClass.AUTH_DROPOFF: [
        "second factor never supplied, session timed out at the payer end",
        "payer left the verification page before confirming",
        "collect notification ignored until it lapsed",
    ],
    FailureClass.EXPIRED_INSTRUMENT: [
        "validity window on the plastic has elapsed",
        "the token references a superseded card record",
    ],
    FailureClass.INVALID_INSTRUMENT: [
        "destination handle could not be resolved by any provider",
        "target account dormant and delinked from the branch",
    ],
    FailureClass.LIMIT_EXCEEDED: [
        "ticket size beyond the sanctioned ceiling for this payer",
        "cumulative usage for the cycle already consumed",
    ],
    FailureClass.RISK_DECLINE_ISSUER: [
        "refused upstream, payer advised to speak with their bank",
        "issuer policy blocked this merchant category for the payer",
    ],
    FailureClass.SUSPECTED_FRAUD: [
        "instrument reported stolen, capture and hold",
        "payer profile matched a known abuse signature",
    ],
    FailureClass.MANDATE_INACTIVE: [
        "recurring authority withdrawn ahead of this cycle",
        "auto debit registration is dormant for this payer",
    ],
    FailureClass.TECHNICAL_ERROR: [
        "processor returned an unmapped response, settlement state unclear",
        "capture request lost in transit, no ledger entry created",
    ],
    FailureClass.CUSTOMER_CANCELLED: [
        "payer dismissed the collect prompt deliberately",
        "checkout abandoned by choice before the debit call",
    ],
}


def _corrupt(text: str, rng: random.Random) -> str:
    """Typos, dropped words, casing, truncation - what real switches emit."""
    words = text.split()
    if len(words) > 4 and rng.random() < 0.5:
        words.pop(rng.randrange(len(words)))
    text = " ".join(words)

    if rng.random() < 0.5 and len(text) > 12:  # single-character typo
        i = rng.randrange(len(text))
        text = text[:i] + rng.choice("abcdefghijklmnopqrstuvwxyz") + text[i + 1:]

    if rng.random() < 0.3:
        text = text.upper()
    if rng.random() < 0.25 and len(text) > 30:
        text = text[: rng.randint(20, len(text) - 1)]
    return text


def build_drift_set(
    payments: List[FailedPayment],
    labels: Dict[str, FailureClass],
    mode: str = "paraphrase",
    seed: int = 11,
) -> Tuple[List[FailedPayment], Dict[str, FailureClass]]:
    """Rewrites a batch's descriptions while keeping the labels intact.

    Structured reason codes are stripped: drift is about the text tail, and
    leaving the contract field in place would let the rules win for free.
    """
    rng = random.Random(seed)
    drifted: List[FailedPayment] = []

    for payment in payments:
        truth = labels[payment.payment_id]
        options = PARAPHRASE.get(truth)
        if not options:
            continue

        if mode == "paraphrase":
            description = rng.choice(options)
        elif mode == "noise":
            description = _corrupt(payment.error_description or rng.choice(options), rng)
        else:
            raise ValueError("unknown drift mode: {}".format(mode))

        drifted.append(
            payment.model_copy(update={"error_reason": None, "error_description": description})
        )

    return drifted, {p.payment_id: labels[p.payment_id] for p in drifted}
