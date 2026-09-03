# Recovery results

> **These are simulated outcomes, not observed ones.** No real payment was recovered. The world model in `sim/outcome_model.py` is deliberately built to disagree with the agent's own beliefs, and the sensitivity sweep below exists because the underlying priors are estimates from public industry reporting rather than measurements from a real merchant. Read the Methodology section before quoting any figure here.

- Batch: **400 payments, INR 980,384 at risk**
- Diagnosis provider: `offline_tfidf`
- 24 replications per policy, paired on common random numbers, world noise sigma = 0.35

## Policy comparison

| Policy | Recovery rate | Recovered | Cost | Net contribution | 90% interval | Contacts | Churned |
|---|---|---|---|---|---|---|---|
| do_nothing | 18.7% | INR 185,211 | INR 0 | **INR 64,824** | INR 36,455 to 81,918 | 0 | 0.0 |
| retry_all | 24.0% | INR 239,239 | INR 8,150 | **INR 75,584** | INR 43,717 to 91,423 | 0 | 0.0 |
| blind_24h | 26.3% | INR 253,601 | INR 8,150 | **INR 80,610** | INR 54,358 to 96,135 | 0 | 0.0 |
| nudge_all | 38.1% | INR 377,380 | INR 3,704 | **INR 128,379** | INR 100,882 to 144,046 | 399 | 12.9 |
| **rebound** | 40.5% | INR 385,569 | INR 482 | **INR 134,467** | INR 108,683 to 152,257 | 146 | 0.0 |

*Net contribution* is recovered value at the merchant's margin, minus what the policy spent to get it - including the penalties a policy incurs for retrying payments that should never be retried, and for churning customers it over-messaged.

## Headline

- Against doing nothing, Rebound adds **INR 69,643** of net contribution on this batch (+107% on a base of INR 64,824), 90% interval INR 46,191 to 80,475, winning in 100% of replications.
- Against the best naive alternative (`nudge_all`), it adds **INR 6,088** (90% interval INR -19,423 to 17,690), winning in 88% of replications.
- It does that while contacting **146** customers, against `nudge_all`'s **399** - 63% fewer messages.
- It stays silent on **88** of 400 payments.

## Sensitivity: how wrong can the agent be and still win?

`sigma` is the spread of the lognormal shock applied to every one of the agent's efficacy beliefs. At 0.8 its estimates are routinely off by a factor of two. Values are mean net contribution in INR.

| sigma | do_nothing | retry_all | blind_24h | nudge_all | rebound | agent rank | nudge_all churn | rebound churn |
|---|---|---|---|---|---|---|---|---|
| 0.0 | 62,704 | 75,677 | 83,501 | 114,659 | 134,549 | **1 of 5** | 12.1 | 0.0 |
| 0.2 | 62,704 | 73,818 | 80,858 | 120,065 | 131,560 | **1 of 5** | 12.1 | 0.0 |
| 0.35 | 62,704 | 72,726 | 78,249 | 126,498 | 129,434 | **1 of 5** | 12.1 | 0.0 |
| 0.5 | 62,704 | 71,441 | 74,618 | 130,611 | 127,150 | **2 of 5** | 12.1 | 0.0 |
| 0.8 | 62,704 | 67,667 | 70,860 | 135,046 | 120,732 | **2 of 5** | 12.1 | 0.0 |

**The honest reading of this table.** Rebound wins on money while its efficacy beliefs are roughly right. Once they are routinely off by half (sigma 0.5 and above), blanket messaging earns more, because a policy that targets badly is worse than one that does not target at all. That is the real boundary of this approach and it is the argument for calibrating against observed outcomes before trusting the policy with a budget.

It is also worth reading the last two columns while you do. Even where `nudge_all` earns more, it gets there by messaging every consenting customer on the list and burning roughly 12 of them per batch. Rebound churns none. A quarter of extra revenue that costs you customers is a loan, not a win - but this harness only scores one batch, so it flatters the blanket policy by construction.

## Methodology and limitations

1. **Simulated, not observed.** Every rupee here comes from a model. The class mix and organic-recovery priors in `taxonomy.py` are estimates assembled from public payment-industry reporting; they are not Razorpay figures and were not fitted to any real dataset.
2. **The world disagrees with the agent on purpose.** Reusing the agent's own efficacy table as ground truth would make it win by construction. Instead the world applies a seeded lognormal shock to every belief, five deliberate directional biases, and a contact-fatigue and churn model the agent does not know exists.
3. **Shared structure is the honest limitation.** The world and the agent still agree on *which factors matter* - root cause, timing, channel, customer history. They disagree only on magnitudes. A world where a completely different mechanism drove recovery would not be captured, and this harness cannot tell you that it exists.
4. **Paired comparison.** Policies are compared on identical per-payment random draws, so the interval reflects genuine policy difference rather than which policy drew the luckier batch.
5. **What would replace this.** A shadow-mode deployment: run the agent alongside a merchant's existing recovery flow, take no actions, and score its decisions against what actually happened. That is the only way to get a number worth putting in a contract.
