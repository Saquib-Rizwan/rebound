# What to fix, not just what to chase

Per-payment recovery treats symptoms. These are the patterns underneath, ranked by the money involved.

Batch: **400 failed payments, INR 980,384 at risk**.

Findings are computed by deterministic grouping and thresholds in `analytics/insights.py` - no model invents a recommendation or a number.

## 1. upi carries 34% of failed value

**INR 335,864** at stake — 34% of failed value · severity medium

157 of failures on this rail, worth INR 335,864, and its single largest cause is auth_dropoff (39% of the rail's failed value).

> **Do this:** Compare authorisation rates across your acquirers on this rail before investing further in recovery. A routing change fixes failures that recovery can only chase after the fact.

## 2. auth_dropoff is 27% of failed value

**INR 263,034** at stake — 27% of failed value · severity high

99 payments worth INR 263,034. Left alone, roughly 22% of these recover on their own, which puts about INR 205,167 genuinely at risk.

> **Do this:** Checkout drop-off is a UX problem before it is a recovery problem. Shorten the OTP step, keep the customer on-page, and pre-fill the instrument.

## 3. insufficient_funds is 21% of failed value

**INR 202,321** at stake — 21% of failed value · severity high

93 payments worth INR 202,321. Left alone, roughly 18% of these recover on their own, which puts about INR 165,903 genuinely at risk.

> **Do this:** Align retries with salary cycles rather than a fixed backoff, and offer a smaller first instalment where the ticket allows it.

## 4. 8% of failed value should not be chased at all

**INR 81,289** at stake — 8% of failed value · severity medium

37 payments worth INR 81,289 were declined for fraud, issuer risk, or deliberate customer cancellation. Retrying these earns fees and chargeback exposure rather than revenue, and messaging the customer is worse than silence.

> **Do this:** Exclude these classes from any recovery campaign, and measure your recovery rate against the addressable base rather than all failures - otherwise the ceiling looks lower than it is and every tool underperforms on paper.

## 5. 3 customers failed 7 or more times

**INR 59,753** at stake — 6% of failed value · severity medium

They account for INR 59,753 of failed value, against a batch average of 3.1 failures per customer. Repeat failure by the same payer is usually a broken saved instrument or a persistent issuer block, not bad luck.

> **Do this:** Route these to a one-time human or assisted flow rather than another automated attempt. Each additional silent retry lowers the odds and, on a contact channel, raises churn risk.

## 6. Bank downtime clusters between 17:00 and 20:00

**INR 34,963** at stake — 31% of failed value · severity high

31% of all downtime-related failure value lands in a three-hour window. That is an issuer availability pattern, not customer behaviour - the same payments would very likely succeed a few hours later.

> **Do this:** Move scheduled debits, subscription renewals and mandate presentments out of 17:00-20:00. For anything that must run then, set the first retry to land after 20:00 rather than the default short backoff.
