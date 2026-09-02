# Classifier evaluation

Scored against held-out labels the classifier never sees (`data/labels.json`, written by the generator and read only here).

> **Read the drift section before trusting the headline table.** The main batch and the regex anchors in `rules.py` were written by the same author, so near-perfect rule precision on it is partly circular. The drift set exists to break that circularity and is the number we actually stand behind.

## Ablation

| Arm | Accuracy | Accuracy when answered | Coverage | Macro F1 | Error cost | Model rows | Net calls | LLM cost | Wall time |
|---|---|---|---|---|---|---|---|---|---|
| rules_only | 92.5% | 100.0% | 92.5% | 0.951 | INR 4,040 | 0 | 0 | $0.0000 | 0.01s |
| model_only | 45.2% | 84.6% | 53.5% | 0.455 | INR 19,314 | 400 | 387 | $0.0000 | 9.21s |
| hybrid | 96.2% | 100.0% | 96.2% | 0.991 | INR 3,286 | 30 | 28 | $0.0000 | 8.95s |

`Model rows` is how many payments were routed to the model. `Net calls` is how many of those actually hit the network - the rest were served from the on-disk response cache, which is why a re-run costs nothing.

> **Degraded rows present.** `model_only` fell back to the offline classifier on 387 of 400 model rows; `hybrid` fell back to the offline classifier on 28 of 30 model rows. The provider refused those calls (free-tier quota), the circuit breaker opened, and the pipeline kept running on the offline classifier. Those arms are a blend of two classifiers and their accuracy should be read as a floor, not as the model's score.

Error cost is the modelled rupee consequence of the mistakes each arm makes, not a count of them. See `backend/rebound/diagnose/error_cost.py` for every assumption behind it.

## Drift: held-out wording the rules were never tuned on

`paraphrase` restates all 12 root causes using none of the anchor phrases in `rules.py`, with the structured reason code stripped. `noise` corrupts the original strings with typos, dropped words, upper-casing and truncation. Both keep the true labels.

| Set | Arm | Accuracy | Coverage | Macro F1 | Error cost | Model rows |
|---|---|---|---|---|---|---|
| paraphrase | rules_only | 0.0% | 1.2% | 0.000 | INR 34,193 | 0 |
| paraphrase | hybrid | 51.7% | 60.5% | 0.454 | INR 12,995 | 395 |
| noise | rules_only | 40.8% | 40.8% | 0.590 | INR 18,369 | 0 |
| noise | hybrid | 56.5% | 64.5% | 0.671 | INR 12,184 | 237 |

This is where the model earns its place. Rule coverage collapses on unseen wording because high-precision anchors are, by construction, brittle to rewording - and the pipeline responds by routing far more rows to the model rather than guessing.

## Per-class detail - hybrid

| Class | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| auth_dropoff | 99 | 1.00 | 1.00 | 1.00 |
| insufficient_funds | 93 | 1.00 | 0.87 | 0.93 |
| bank_downtime | 44 | 1.00 | 0.93 | 0.96 |
| technical_error | 32 | 1.00 | 1.00 | 1.00 |
| invalid_instrument | 28 | 1.00 | 1.00 | 1.00 |
| mandate_inactive | 28 | 1.00 | 1.00 | 1.00 |
| expired_instrument | 25 | 1.00 | 1.00 | 1.00 |
| risk_decline_issuer | 18 | 1.00 | 1.00 | 1.00 |
| limit_exceeded | 14 | 1.00 | 1.00 | 1.00 |
| customer_cancelled | 12 | 1.00 | 1.00 | 1.00 |
| suspected_fraud | 7 | 1.00 | 1.00 | 1.00 |
| unknown | 0 | 0.00 | 0.00 | 0.00 |

## Confusion matrix - hybrid

| true \ predicted | auth_dropoff | bank_downtime | customer_cancelled | expired_instrument | insufficient_funds | invalid_instrument | limit_exceeded | mandate_inactive | risk_decline_issuer | suspected_fraud | technical_error | unknown |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **auth_dropoff** | 99 |  |  |  |  |  |  |  |  |  |  |  |
| **bank_downtime** |  | 41 |  |  |  |  |  |  |  |  |  | 3 |
| **customer_cancelled** |  |  | 12 |  |  |  |  |  |  |  |  |  |
| **expired_instrument** |  |  |  | 25 |  |  |  |  |  |  |  |  |
| **insufficient_funds** |  |  |  |  | 81 |  |  |  |  |  |  | 12 |
| **invalid_instrument** |  |  |  |  |  | 28 |  |  |  |  |  |  |
| **limit_exceeded** |  |  |  |  |  |  | 14 |  |  |  |  |  |
| **mandate_inactive** |  |  |  |  |  |  |  | 28 |  |  |  |  |
| **risk_decline_issuer** |  |  |  |  |  |  |  |  | 18 |  |  |  |
| **suspected_fraud** |  |  |  |  |  |  |  |  |  | 7 |  |  |
| **technical_error** |  |  |  |  |  |  |  |  |  |  | 32 |  |

## Most expensive possible confusions

At a reference ticket of INR 1,000. These drive what the test suite guards.

| True | Predicted | Cost |
|---|---|---|
| suspected_fraud | insufficient_funds | INR 1,521 |
| suspected_fraud | bank_downtime | INR 1,521 |
| suspected_fraud | limit_exceeded | INR 1,521 |
| suspected_fraud | technical_error | INR 1,521 |
| technical_error | suspected_fraud | INR 119 |
| technical_error | mandate_inactive | INR 105 |

## Reading this

- Batch at risk: INR 980,384 across 400 payments.
- Hybrid settles 92% of rows with deterministic rules at zero marginal cost; the model is invoked only on the remainder.
- `Coverage` below 100% is deliberate. Rows below the confidence floor, and rows whose gateway text contained injection markers, are demoted to `unknown`, which the policy engine never retries and never contacts.
- Rows demoted this run: 15 low-confidence, 0 hostile.
