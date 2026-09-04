# Five-minute pitch — script and shot list

Recording notes: screen capture at 1600×1000 or larger, dashboard already open at
`http://localhost:8000/app/`, terminal in the same workspace, and **run
`python rebound.py run --provider offline --run-id demo` once before recording** so
the numbers on screen match what you say.

Total: 4:50. Leave ten seconds of headroom.

---

## 0:00 – 0:35 — The problem, and the unusual claim

**On screen:** the dashboard, Decisions tab, already loaded.

> "About one in ten online payments fails. Card declined, bank down, customer didn't
> finish the OTP. Most merchants just let that money go, because chasing it properly
> is hard.
>
> This is Rebound. It's a payment recovery agent, and the thing I want you to notice
> first is this number —" **[point at `CHOSE SILENCE 90`]** — "on 90 of these 400
> payments, it decided to do nothing at all. It contacted 148 customers out of 400
> failures.
>
> Most recovery tools act on everything. The interesting engineering problem isn't
> chasing failed payments. It's knowing which ones not to chase."

**Why this opening:** you have thirty seconds before a reviewer decides whether this
is another AI wrapper. Leading with restraint rather than with a big number is the
fastest way to signal that you thought about the problem instead of the demo.

---

## 0:35 – 1:20 — What it actually does

**On screen:** click the highlighted row, so the right panel fills.

> "For every failed payment it does four things: works out why it died, prices every
> possible response in rupees, checks what it's actually allowed to do, and then acts
> — or doesn't.
>
> Here's a real decision." **[right panel]** "₹17,482, customer abandoned the OTP.
> The agent priced seven options. It's sending a WhatsApp payment link, because at a
> 44% recovery chance that's ₹2,678 of expected margin against ₹13 of cost.
>
> And every option it *rejected* is stored too — that's the point of the audit trail.
> A merchant doesn't ask why you sent a message. They ask why you didn't."

**Then click the `suppress` filter, and open any row.**

> "Here's one it refused. The best action was worth ₹2,400 and it did nothing, because
> the send time fell inside quiet hours and the fallback hit the customer's cooldown."

---

## 1:20 – 2:10 — Where the AI is, and where it deliberately isn't

**On screen:** scroll the decisions list so the green `rules` / violet `gemini` labels
are visible, then switch to your terminal.

> "Now the part I'd want to be asked about. Look at the source column — green means a
> deterministic rule decided it, violet means the language model did.
>
> Two thirds of gateway failures come with a structured error code. That's a contract.
> Reading a contract with an LLM is waste. So rules run first, and they abstain rather
> than guess — anything ambiguous falls through to the model."

**Show `reports/classifier.md`, the ablation table.**

> "Running the model on everything: 96.7% accuracy, one call per payment. Rules first,
> model only on the tail: 96.8% accuracy, 0.075 calls per payment.
>
> Same accuracy. Thirteen times fewer AI calls. That's measured, not asserted."

**Scroll to the drift section.**

> "And here's why the model still earns its place. When I rewrite the same failures in
> wording the rules have never seen, the rules go to zero percent. Regex doesn't
> survive a reworded bank message."

---

## 2:10 – 3:00 — The money, and the honest limit

**On screen:** the Results tab.

> "Five policies on the same 400 payments, same random draws, forty replications.
> Against doing nothing, Rebound adds ₹67,487 of net contribution — and it won in
> 100 out of 100 runs.
>
> But look at this one." **[point at `nudge_all`]** "Messaging every single customer
> makes almost as much money. We're ahead by ₹5,482 and the confidence interval
> crosses zero. I'm not going to claim we beat it on revenue, because we don't.
>
> What we do is get there with 148 messages instead of 399, and zero churned customers
> against about thirteen. If you only measure rupees for one quarter, spraying everyone
> looks fine. The bill arrives later."

**Point at the sensitivity table.**

> "And this is where it stops working. Once the agent's estimates are wrong by half, it
> loses to blanket messaging — because a policy that targets badly is worse than one
> that doesn't target at all. That's the honest boundary."

---

## 3:00 – 3:45 — What broke

**On screen:** `POSTMORTEM.md`, entry 1.

> "Eight things broke while building this. The first one nearly invalidated the whole
> project.
>
> My rules scored 100% precision. That felt great for about a minute and then it felt
> wrong — because I'd written both the test data *and* the rules, so they were being
> graded against their own answer key.
>
> So I built a held-out set that restates every failure in different words. Accuracy
> went from 92.5% to zero. That failure is the reason the drift number you just saw
> exists at all."

**Scroll to entry 5, briefly.**

> "Number five is a lognormal that wasn't mean-preserving, which had silently inverted
> a robustness conclusion I'd already written down. Six and seven only turned up once
> I pointed the thing at real Razorpay webhooks."

---

## 3:45 – 4:30 — It's real, and it's hostile-input safe

**On screen:** terminal.

```bash
python rebound.py verify-gateway
```

> "This is a real Razorpay test-mode API call — a genuine payment link. I've also run
> it against live webhooks from Razorpay's own infrastructure: a real `payment.failed`
> came in, signature verified, Gemini classified it as bank downtime, and the agent
> scheduled a retry an hour out rather than immediately, because retrying now hits the
> same dead bank."

```bash
python rebound.py simulate-webhook --bad-signature
```

> "Forged signature, rejected."

**Then the injection command from JUDGES.md.**

> "And the bank's error text is untrusted input. Here's an attacker asking for fifty
> retries on a ₹9,000 payment." **[show the output]** "It got demoted to unknown and
> escalated to a human. The model can only return a value from a closed enum — it
> physically cannot express an action."

---

## 4:30 – 4:50 — Close

**On screen:** the README, or the terminal with `pytest` finishing.

> "69 tests, running in CI with no API keys configured on purpose, so every gate holds
> with no model at all. It also enforces RBI's e-mandate rules — a ₹40,000 recurring
> debit can't be silently retried, because that isn't allowed, and that compliance
> costs us ₹1,130 which I report rather than hide.
>
> Everything except the webhook recoveries is simulated, and the README says so before
> it claims a single number. The thing that would replace the simulator is shadow mode.
>
> Thanks."

---

# The three questions they will ask

### 1. "Your numbers are simulated. Why should I believe any of them?"

> "You shouldn't believe the absolute numbers, and I don't claim them. What the harness
> supports is a relative comparison: five policies, identical random draws, forty
> replications, under a world model I deliberately built to disagree with the agent —
> mean-preserving noise on every belief, five directional biases, and a churn model the
> agent doesn't know exists. If I'd used the agent's own efficacy table as ground truth
> it would win by construction, and that's the mistake I was most worried about making.
>
> What would replace it is shadow mode: run alongside a merchant's real flow, take no
> actions, score the decisions against what actually happened. That's the only number
> worth putting in a contract, and it's the first thing in my future work section."

### 2. "How much of this did AI write?"

> "A lot of the code. What's mine is the design decisions and the things I chose to
> distrust. The most valuable hour I spent was deleting a 100% precision result because
> I didn't believe it — and being right. Same with the sensitivity sweep: I'd already
> written the conclusion when I found the distribution bug that had produced it.
>
> An assistant will happily write you a confident wrong answer. The judgment I'd bring
> to a team is knowing which good result to go back and attack."

### 3. "Why is the agent's biggest feature that it does nothing?"

> "Because doing nothing is the correct answer 22% of the time, and it's the one thing
> a recovery tool optimising for recovery rate will never do.
>
> It's not a rule I wrote. Suppress is priced at exactly zero, so any action with a
> negative expected return loses to silence arithmetically. That falls out of putting
> a real cost on interrupting a customer — and it's why we churn nobody while blanket
> messaging churns about thirteen customers per four hundred payments."

---

## Recording checklist

- [ ] `python rebound.py run --provider offline --run-id demo` — so the screen matches the script
- [ ] `python rebound.py serve` running, dashboard open at the Decisions tab
- [ ] Terminal font large enough to read at 1080p
- [ ] Close Slack, email, and anything with notifications
- [ ] Record once end to end before trying to make it perfect
- [ ] Watch it back with the sound off — if the screen alone doesn't tell the story, fix the shots, not the words
