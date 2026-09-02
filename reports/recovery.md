# Recovery results

> **These are simulated outcomes, not observed ones.** No real payment was recovered. The world model in `sim/outcome_model.py` is deliberately built to disagree with the agent's own beliefs, and the sensitivity sweep below exists because the underlying priors are estimates from public industry reporting rather than measurements from a real merchant. Read the Methodology section before quoting any figure here.

- Batch: **400 payments, INR 980,384 at risk**
- Diagnosis provider: `offline_tfidf`
- 40 replications per policy, paired on common random numbers, world noise sigma = 0.35

## Policy comparison

| Policy | Recovery rate | Recovered | Cost | Net contribution | 90% interval | Contacts | Churned |
|---|---|---|---|---|---|---|---|
| do_nothing | 18.7% | INR 190,015 | INR 0 | **INR 66,505** | INR 40,693 to 86,311 | 0 | 0.0 |
| retry_all | 23.8% | INR 244,648 | INR 8,150 | **INR 77,477** | INR 46,743 to 99,673 | 0 | 0.0 |
| blind_24h | 25.9% | INR 254,147 | INR 8,150 | **INR 80,802** | INR 53,949 to 101,811 | 0 | 0.0 |
| nudge_all | 37.7% | INR 378,119 | INR 3,831 | **INR 128,510** | INR 102,529 to 149,295 | 399 | 13.4 |
| **rebound** | 40.1% | INR 387,441 | INR 482 | **INR 135,122** | INR 111,799 to 156,802 | 146 | 0.0 |

*Net contribution* is recovered value at the merchant's margin, minus what the policy spent to get it - including the penalties a policy incurs for retrying payments that should never be retried, and for churning customers it over-messaged.

## Headline

- Against doing nothing, Rebound adds **INR 68,617** of net contribution on this batch (+103% on a base of INR 66,505), 90% interval INR 48,132 to 80,475, winning in 100% of replications.
- Against the best naive alternative (`nudge_all`), it adds **INR 6,612** (90% interval INR -7,207 to 17,690), winning in 88% of replications.
- It does that while contacting **146** customers, against `nudge_all`'s **399** - 63% fewer messages.
- It stays silent on **88** of 400 payments.

## Sensitivity: how wrong can the agent be and still win?

`sigma` is the spread of the lognormal shock applied to every one of the agent's efficacy beliefs. At 0.8 its estimates are routinely off by a factor of two. Values are mean net contribution in INR.

| sigma | do_nothing | retry_all | blind_24h | nudge_all | rebound | agent rank | nudge_all churn | rebound churn |
|---|---|---|---|---|---|---|---|---|
| 0.0 | 64,775 | 77,210 | 86,173 | 115,507 | 136,014 | **1 of 5** | 12.6 | 0.0 |
| 0.2 | 64,775 | 75,532 | 82,965 | 121,161 | 133,057 | **1 of 5** | 12.6 | 0.0 |
| 0.35 | 64,775 | 74,635 | 80,607 | 127,387 | 131,546 | **1 of 5** | 12.6 | 0.0 |
| 0.5 | 64,775 | 73,162 | 76,236 | 132,818 | 129,504 | **2 of 5** | 12.6 | 0.0 |
| 0.8 | 64,775 | 68,859 | 72,566 | 136,503 | 123,503 | **2 of 5** | 12.6 | 0.0 |

**The honest reading of this table.** Rebound wins on money while its efficacy beliefs are roughly right. Once they are routinely off by half (sigma 0.5 and above), blanket messaging earns more, because a policy that targets badly is worse than one that does not target at all. That is the real boundary of this approach and it is the argument for calibrating against observed outcomes before trusting the policy with a budget.

It is also worth reading the last two columns while you do. Even where `nudge_all` earns more, it gets there by messaging every consenting customer on the list and burning roughly 13 of them per batch. Rebound churns none. A quarter of extra revenue that costs you customers is a loan, not a win - but this harness only scores one batch, so it flatters the blanket policy by construction.

## Methodology and limitations

1. **Simulated, not observed.** Every rupee here comes from a model. The class mix and organic-recovery priors in `taxonomy.py` are estimates assembled from public payment-industry reporting; they are not Razorpay figures and were not fitted to any real dataset.
2. **The world disagrees with the agent on purpose.** Reusing the agent's own efficacy table as ground truth would make it win by construction. Instead the world applies a seeded lognormal shock to every belief, five deliberate directional biases, and a contact-fatigue and churn model the agent does not know exists.
3. **Shared structure is the honest limitation.** The world and the agent still agree on *which factors matter* - root cause, timing, channel, customer history. They disagree only on magnitudes. A world where a completely different mechanism drove recovery would not be captured, and this harness cannot tell you that it exists.
4. **Paired comparison.** Policies are compared on identical per-payment random draws, so the interval reflects genuine policy difference rather than which policy drew the luckier batch.
5. **What would replace this.** A shadow-mode deployment: run the agent alongside a merchant's existing recovery flow, take no actions, and score its decisions against what actually happened. That is the only way to get a number worth putting in a contract.
