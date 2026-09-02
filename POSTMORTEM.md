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
