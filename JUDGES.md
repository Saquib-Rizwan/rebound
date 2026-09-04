# For the reviewer

You are reading dozens of these. This page exists so you can evaluate mine quickly
and, if you want to, attack it efficiently.

---

## If you would rather not clone anything

**[The dashboard is live here](https://saquib-rizwan.github.io/rebound/demo/)** — a frozen snapshot of one run, served as
static files. Every decision, every audit trail, the policy comparison and the
systemic findings, with no backend to be down.

---

## Sixty seconds

**Rebound is a payment-recovery agent whose most distinctive behaviour is refusing to
act.** On 400 failed payments worth ₹9,80,384 it stays silent on 90 of them, contacts
148 customers rather than 399, spends ₹716, and adds **₹67,487** of net contribution
against doing nothing — 90% CI [₹45,927, ₹80,204], winning 100 of 100 replications.

Three things I would look at if I were you:

| | Where |
|---|---|
| The agent decided *not* to act, and can explain why | `python rebound.py serve` → Decisions tab → filter `suppress` → click a row |
| Its own evaluation was circular, and I caught it | [POSTMORTEM.md](POSTMORTEM.md) entry 1 |
| It refuses illegal retries, not just unprofitable ones | [tests/test_compliance.py](tests/test_compliance.py) — RBI e-mandate rails |

**Everything below is simulated except where explicitly marked `observed`.** No real
payment was recovered. That is stated in the README before any number is claimed.

---

## Five minutes, from a cold clone

```bash
pip install -r requirements.txt
python rebound.py generate                              # 400 payments, 33% unstructured
python rebound.py run --provider offline --run-id demo  # the agent decides
python rebound.py eval-policy --replications 40         # the money, vs 4 baselines
python -m pytest -q                                     # 69 tests, no network, no keys
```

No API keys, no environment variables, no virtualenv setup. If any of that fails on a
clean machine, that is a defect and I would want to know.

Then `python rebound.py serve` and open <http://localhost:8000>.

---

## Where to find evidence for each criterion

### Problem taste
Failed payments are Razorpay's own largest merchant pain, and the interesting part is
not chasing them — it is knowing which ones *not* to chase. The agent treats silence
as a first-class action priced at exactly zero, so anything with negative expected
value loses to doing nothing arithmetically rather than by a rule someone remembered
to write. `python rebound.py insights` goes one level further and reports what to
*fix* rather than what to chase.

### Build quality
`backend/rebound/` is organised by pipeline stage, not by type. 69 tests covering
safety invariants, adversarial security, RBI compliance, quality gates, calibration,
and the README's own numbers. CI runs them **with no credentials configured on
purpose**, so every gate holds with no model at all and a failing key can never be why
the build is green.

### AI judgment
The model runs on **7.5% of traffic**. Deterministic rules handle the rest and abstain
freely rather than guessing. The ablation in [reports/classifier.md](reports/classifier.md):

| Arm | Accuracy | Error cost | Model calls per payment |
|---|---|---|---|
| rules only | 92.5% | ₹4,040 | 0 |
| model only | 96.7% | ₹55 | 1.0 |
| **hybrid** | **96.8%** | **₹116** | **0.075** |

Same accuracy as model-on-everything, at a thirteenth of the calls. The drift section
of that report shows why the model is still necessary: on reworded bank text the rules
score **0.0%**.

### Failure recovery
[POSTMORTEM.md](POSTMORTEM.md), eight entries, written during the build rather than
reconstructed. Two were found only because the system was pointed at real Razorpay
webhooks. One inverted a conclusion I had already written down.

---

## If I were reviewing this, here is what I would attack

I would rather you have these than find them yourself and conclude I had not.

**"Your recovery numbers are simulated, so they mean nothing."**
Largely fair. What the simulation can support is a *relative* claim — a bounded
expected-value policy against four alternatives, on identical random draws, under a
world model deliberately built to disagree with the agent's own beliefs. What it
cannot support is any absolute recovery rate for a real merchant. The methodology
section of [reports/recovery.md](reports/recovery.md) says exactly this, and names
shadow mode as the only thing that would replace it.

**"You wrote the data and the rules, so of course the rules score well."**
Correct, and it is why the drift set exists. On held-out wording the rules drop from
92.5% to **0.0%**. That number is in the report because it is the honest one.

**"Your agent barely beats messaging everyone."**
True. +₹5,482 with a confidence interval that crosses zero. It is not richer, it is
quieter: 148 contacts against 399, and zero churned customers against roughly 13. If
you only measure rupees for one quarter, blanket messaging looks fine.

**"The RBI guardrails are decoration."**
They cost ₹1,130 of net contribution, which is reported rather than hidden, and
`tests/test_compliance.py` asserts a ₹40,000 e-mandate can never be silently retried
and that the rails do not leak onto one-off payments.

**"How much of this did an AI write?"**
A lot of the code. The design decisions, the things I chose to distrust, and every
entry in the postmortem are mine — including the two occasions I threw away a result
that looked good because I did not believe it. I would rather be asked about entry 1
or entry 5 than about any feature in the repo.

---

## What is genuinely missing

- **Shadow mode.** Until this runs alongside a real merchant flow and is scored
  against what actually happened, every recovery figure here is a model output.
- **Per-merchant policy.** The constants in `config.py` are global; risk appetite and
  contact tolerance are not.
- **A scheduler runtime.** `execute/scheduler.py` fires due actions and re-checks
  guardrails correctly, but it has to be invoked. Production wants a durable queue
  with retries and a dead-letter path.
- **Calibration is global.** Pooling an edtech merchant with a grocery one averages
  away the signal that matters.
