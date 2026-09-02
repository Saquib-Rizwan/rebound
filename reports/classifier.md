# Classifier evaluation

Scored against held-out labels the classifier never sees (`data/labels.json`, written by the generator and read only here).

> **Read the drift section before trusting the headline table.** The main batch and the regex anchors in `rules.py` were written by the same author, so near-perfect rule precision on it is partly circular. The drift set exists to break that circularity and is the number we actually stand behind.

### Coverage caps applied to this run

- `model_only` was scored on the first 150 of 400 payments to stay inside the free-tier quota; every other main-batch arm used all 400.
- `paraphrase` drift set scored on 150 of 400 rows (same rows for both arms, so the comparison is like for like).
- `noise` drift set scored on 150 of 400 rows (same rows for both arms, so the comparison is like for like).

## Ablation

| Arm | Accuracy | Accuracy when answered | Coverage | Macro F1 | Error cost | Model rows | Net calls | LLM cost | Wall time |
|---|---|---|---|---|---|---|---|---|---|
| rules_only | 92.5% | 100.0% | 92.5% | 0.951 | INR 4,040 | 0 | 0 | $0.0000 | 0.00s |
| model_only | 62.0% | 89.4% | 69.3% | 0.712 | INR 4,225 | 150 | 150 | $0.0036 | 172.11s |
| hybrid | 96.8% | 96.8% | 100.0% | 0.978 | INR 116 | 30 | 23 | $0.0017 | 87.14s |

`Model rows` is how many payments were routed to the model. `Net calls` is how many of those actually hit the network - the rest were served from the on-disk response cache, which is why a re-run costs nothing.

> **Degraded rows present.** `model_only` fell back to the offline classifier on 100 of 150 model rows. The provider refused those calls (free-tier quota), the circuit breaker opened, and the pipeline kept running on the offline classifier. Those arms are a blend of two classifiers and their accuracy should be read as a floor, not as the model's score.

Error cost is the modelled rupee consequence of the mistakes each arm makes, not a count of them. See `backend/rebound/diagnose/error_cost.py` for every assumption behind it.

## Drift: held-out wording the rules were never tuned on

`paraphrase` restates all 12 root causes using none of the anchor phrases in `rules.py`, with the structured reason code stripped. `noise` corrupts the original strings with typos, dropped words, upper-casing and truncation. Both keep the true labels.

| Set | Arm | Accuracy | Coverage | Macro F1 | Error cost | Model rows |
|---|---|---|---|---|---|---|
| paraphrase | rules_only | 0.0% | 0.7% | 0.000 | INR 12,469 | 0 |
| paraphrase | hybrid | 62.0% | 66.7% | 0.548 | INR 4,424 | 149 |
| noise | rules_only | 40.0% | 40.0% | 0.529 | INR 6,827 | 0 |
| noise | hybrid | 74.7% | 84.7% | 0.791 | INR 2,835 | 90 |

This is where the model earns its place. Rule coverage collapses on unseen wording because high-precision anchors are, by construction, brittle to rewording - and the pipeline responds by routing far more rows to the model rather than guessing.

## Per-class detail - hybrid

| Class | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| auth_dropoff | 99 | 0.99 | 1.00 | 0.99 |
| insufficient_funds | 93 | 1.00 | 0.87 | 0.93 |
| bank_downtime | 44 | 0.85 | 1.00 | 0.92 |
| technical_error | 32 | 0.89 | 1.00 | 0.94 |
| invalid_instrument | 28 | 1.00 | 1.00 | 1.00 |
| mandate_inactive | 28 | 1.00 | 1.00 | 1.00 |
| expired_instrument | 25 | 1.00 | 1.00 | 1.00 |
| risk_decline_issuer | 18 | 1.00 | 0.94 | 0.97 |
| limit_exceeded | 14 | 1.00 | 1.00 | 1.00 |
| customer_cancelled | 12 | 1.00 | 1.00 | 1.00 |
| suspected_fraud | 7 | 1.00 | 1.00 | 1.00 |

## Confusion matrix - hybrid

| true \ predicted | auth_dropoff | bank_downtime | customer_cancelled | expired_instrument | insufficient_funds | invalid_instrument | limit_exceeded | mandate_inactive | risk_decline_issuer | suspected_fraud | technical_error |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **auth_dropoff** | 99 |  |  |  |  |  |  |  |  |  |  |
| **bank_downtime** |  | 44 |  |  |  |  |  |  |  |  |  |
| **customer_cancelled** |  |  | 12 |  |  |  |  |  |  |  |  |
| **expired_instrument** |  |  |  | 25 |  |  |  |  |  |  |  |
| **insufficient_funds** | 1 | 7 |  |  | 81 |  |  |  |  |  | 4 |
| **invalid_instrument** |  |  |  |  |  | 28 |  |  |  |  |  |
| **limit_exceeded** |  |  |  |  |  |  | 14 |  |  |  |  |
| **mandate_inactive** |  |  |  |  |  |  |  | 28 |  |  |  |
| **risk_decline_issuer** |  | 1 |  |  |  |  |  |  | 17 |  |  |
| **suspected_fraud** |  |  |  |  |  |  |  |  |  | 7 |  |
| **technical_error** |  |  |  |  |  |  |  |  |  |  | 32 |

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
- Rows demoted this run: 0 low-confidence, 0 hostile.
