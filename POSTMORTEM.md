# What broke, and how we got out

A running log kept **during** the build, not reconstructed afterwards. Entries are
appended as things fail. Nothing here is tidied up in hindsight - the point of the
document is the reasoning, including the reasoning that turned out to be wrong.

---

## 1. Our own evaluation was circular, and it flattered us badly

**Phase 2. Severity: would have invalidated the headline metric.**

The deterministic rules scored 92.5% coverage at **100% precision** on the batch.
That felt good for about a minute, and then it felt wrong. Perfect precision on a
messy real-world signal is not a result, it is a smell.

The cause was structural: the same author wrote `sim/generator.py` (which emits the
gateway error strings) and `diagnose/rules.py` (which regex-matches them). The
rules were, in effect, tested against their own answer key. Every anchor phrase the
rules look for was a phrase the generator had been told to produce.

**What we did.** Built `sim/drift.py`, a held-out test set the rules were never
tuned on. Two modes: `paraphrase` restates all 12 root causes using none of the
anchor vocabulary and strips the structured reason code, and `noise` corrupts the
original strings with typos, dropped words, upper-casing and truncation.

**Result, on unseen wording:**

| Set | Rules-only accuracy | Rules-only coverage |
|---|---|---|
| main (self-authored) | 92.5% | 92.5% |
| noise | 40.8% | 40.8% |
| paraphrase | **0.0%** | **1.2%** |

Regex anchors do not survive a reworded bank message. At all.

**Why this made the product better rather than worse.** It converted the case for
having a model in the pipeline from an assertion into a measurement. On the
paraphrase set the pipeline routes 395 of 400 rows to the model instead of
guessing, because the rules correctly *abstain* rather than firing wrongly - the
high-precision-and-freely-abstain design held up exactly as intended under a
distribution it had never seen. If the rules had degraded silently into confident
wrong answers, the policy engine downstream would have acted on them.

The main-batch numbers stay in the report, clearly labelled as partly circular. The
drift numbers are the ones we stand behind.

---

## 2. The ablation table reported zero model usage for the hybrid arm

**Phase 2. Severity: cosmetic, but it was about to go in the pitch.**

The ablation printed `hybrid: 0 LLM calls`, which would have been a lovely claim
and a false one. Cause: the on-disk response cache is keyed by prompt hash and
shared across arms, and the `model_only` arm runs first. By the time the `hybrid`
arm asked about its 30 tail rows, every answer was already cached, so the counter
that tracks *network* calls never moved.

Both numbers are real, they just answer different questions. Split them:
`model_rows` (how many payments were routed to the model) and `llm_calls` (how
many of those actually hit the network). The report now prints both and explains
the difference, because "our agent made zero API calls" is exactly the kind of
number a panel should push back on.

---

## 3. Writing files through the shell silently truncated at ~200 lines

**Phase 1/2. Severity: annoying, zero product impact.**

Heredoc writes above roughly 200 lines were being cut mid-content by the Windows
shell layer, leaving an unterminated quote and a parse error rather than a partial
file. Failing loudly was lucky - a silent partial write of `taxonomy.py` would have
been a genuinely confusing bug to chase. Switched to direct file writes for
anything large.

---

## 4. We burned a whole day of free-tier quota in about ninety seconds

**Phase 2/3. Severity: lost an afternoon of live-model results.**

The classifier ablation was launched across the full batch on eight worker threads:
three arms on 400 payments plus two drift sets, roughly 1,060 model calls, fired as
fast as the pool could go.

Two things went wrong at once, and the second was hidden by the first.

**Thundering herd.** Eight workers with no client-side pacing do not discover a rate
limit gracefully - they all fire, all get `429`, all sleep on the same backoff
schedule, and all wake up together to do it again. The retry policy was
synchronising the failure instead of spreading it.

**Silent degradation.** When retries were exhausted the pipeline fell back to the
offline classifier, exactly as designed. But nothing *counted* that. The run would
have reported a single accuracy number for an arm where 387 of 400 rows were
actually scored by a completely different classifier. That is a much worse bug than
the crash, because it produces a plausible number instead of an error.

**What we changed.**

1. A shared token-bucket `RateLimiter` paced at the published requests-per-minute,
   so the pool self-limits rather than being refused.
2. A circuit breaker: after five consecutive quota rejections the provider is
   abandoned for the rest of the run instead of being retried into the ground.
3. `degraded_rows` is now counted and printed. Any arm where the provider dropped
   out says so in the report, and its accuracy is labelled a floor rather than a
   score.
4. The model-heavy arms are capped (default 150 rows) with the cap written into the
   report. A truncated study that does not disclose the truncation reads as full
   coverage.

**Also learned, the boring way:** `gemini-2.5-flash` is closed to new API keys, and
`gemini-3.5-flash` carries a much smaller free allowance than the `-lite` models.
Defaulted to `gemini-3.5-flash-lite` and made the model, API version and rate limit
all environment-configurable, because that assumption will rot again.

**What saved us:** the on-disk response cache. Every answer already paid for is
still on disk, so re-running the evaluation costs nothing and the numbers reproduce
without a network at all.

---

## 5. The sensitivity sweep was measuring the wrong thing, and it inverted a conclusion

**Phase 5. Severity: would have shipped a false claim about robustness.**

The recovery harness perturbs the world's parameters to ask *"how wrong can the
agent's beliefs be before it stops beating the naive alternatives?"* Each of the
agent's efficacy numbers is multiplied by a random shock, and sigma controls how
big the shock is.

The first version used `exp(N(0, sigma))`. That is a lognormal, and a lognormal
with mean parameter zero does **not** have mean 1 - it has mean `exp(sigma^2/2)`.
At sigma 0.8 that is 1.38.

So every step of the "sensitivity" sweep was quietly making the simulated world
**38% easier to recover in**, on top of adding noise. The sweep was not measuring
robustness to miscalibration at all. It was measuring which policy benefits most
from a world where every intervention suddenly works better - and the answer to
that question is always "the one that intervenes the most", which is
`nudge_all`. The table showed the agent losing at high sigma and the reason was
arithmetic, not strategy.

**Fix:** use a mean-preserving shock, `exp(N(-sigma^2/2, sigma))`, whose
expectation is exactly 1 at every sigma. Verified numerically over 200k draws
before trusting it.

**The uncomfortable part.** After the fix, the agent *still* drops to second place
at sigma 0.5 and above. The bug was real and the conclusion it produced was also,
as it turns out, roughly right - for a completely different reason. A policy that
targets badly really is worse than one that does not target at all, and that is now
stated in the report as a finding rather than hidden as an artifact.

Two things this cost us and one it bought:

- We nearly published a robustness claim built on a distributional mistake.
- We nearly published the *opposite* claim after fixing it, having assumed the
  finding would disappear along with the bug.
- It produced the most useful sentence in the whole report: we can now say exactly
  where this approach stops working, and why calibration against observed outcomes
  is the thing that has to happen before anyone trusts it with a budget.

---

## 6. Every recovery was being filed against the wrong payment

**Live webhook testing. Severity: the outcome data was silently worthless.**

Paying a recovery link fires `payment_link.paid`. That event carries two payment
identities, and we picked the wrong one.

The bug is that paying a link does not resurrect the failed payment - it creates a
**brand new payment with a new id**. So the event looks like this:

```
failed payment we diagnosed and acted on : pay_TXTJl4MltsmWKX
new payment created by paying the link   : pay_TXTLJT9urehMCF
```

`event_payment_id` read `payload.payment.entity.id` first, which is the *new* one.
Every observed recovery was therefore written against a payment id the system had
never seen, orphaned from the decision that earned it:

```
pay_TXTLJT9urehMCF   INR 499.00   observed   linked to a decision: False
```

No error, no exception, a perfectly plausible-looking row in the outcomes table.
It would have quietly destroyed the only measured data in the project - we would
have had a growing pile of recoveries that could never be attributed to any action
the agent took, while the recoveries the agent actually caused looked like zero.

**The fix.** Read the link's `reference_id` first. We stamp `rebound_<original id>`
into every link we create precisely so a payment on it can be traced back, and the
code just was not consulting it. Also added a guard: an outcome that cannot be
resolved to a payment we made a decision about is recorded as `unattributed`
rather than becoming an outcome row. Counting a stranger's successful payment as
our own recovery is the single easiest way to fake a good result, including by
accident.

**Why this one matters most.** Our own `simulate-webhook` command constructed the
event with `reference_id` and `payment.id` set to the *same* value, because that
seemed natural when writing a fixture. So the bug was invisible in every simulated
test and appeared within seconds of the first real Razorpay event. The fixture
encoded an assumption about the world instead of testing it - which is the whole
argument for having done the live integration rather than stopping at the
simulator.

---

## 7. Two experiments quietly became one

**Feature work on calibration. Severity: silently corrupted a comparison.**

To show that exploration finds action-arms that pure exploitation never tries, we ran
the same batch twice — once exploiting, once with Thompson sampling — under two run
ids, and compared which arms each had touched.

The first numbers were nonsense: the exploit run showed 34 actions where it should
have shown roughly 300, and the arm counts moved between queries of the *same* run.

`decision_id` was a hash of payment id, policy version, timestamp and chosen action.
It did not include the run id. Since the decision timestamp is derived from the
payment's own `created_at`, replaying the same batch produced **identical decision
ids**, and `INSERT OR REPLACE` therefore did not insert a second set of rows - it
rewrote the first run's rows and relabelled them with the new run id.

The second experiment was eating the first one. Nothing errored, and the resulting
table looked entirely plausible.

**Fix:** `run_id` is part of the hash. Deliberately *not* added to `idempotency_key`,
which identifies an outbound action rather than a record: two runs of the same batch
must still be prevented from messaging a customer twice, and that property depends on
the key colliding across runs. The two ids answer different questions and now do so
correctly.

**What made it findable:** the numbers were absurd rather than merely wrong. A subtler
version of this bug - say, a 10% overlap instead of a total overwrite - would have
produced a believable table and shipped.

---

## 8. The fallback path could crash

**Test writing. Severity: latent, but in the worst possible place.**

`_parse_verdict` in `diagnose/llm.py` handles model output that is not what we asked
for. A test fed it `"[]"` — valid JSON, wrong shape — and it raised
`AttributeError: 'list' object has no attribute 'get'`.

This is the *fallback* path. It exists precisely to absorb a model behaving badly, so
it is the one function in the pipeline that must never raise. A model returning a bare
list instead of an object is not exotic; it is an ordinary Tuesday.

**Fix:** check `isinstance(data, dict)` before reading fields, and degrade to
`UNKNOWN` like every other malformed case.

**Worth noting:** we had been running this code against a live model for two days
without hitting it. The test found it in under a second. That is the argument for
writing tests for the paths you believe are already safe, rather than only for the
ones you are unsure about.
