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
