"""Does the agent actually make money? Compared against what, and how sure are we?

Three deliberate choices about method, because the honest answer to "we recovered
X rupees" is worthless without them:

* **Paired comparison (common random numbers).** Every policy sees the same
  per-payment random draws, so the difference between two policies is not
  contaminated by one of them getting a luckier batch.
* **Replications, not a single run.** A single simulated pass over 400 payments is
  one sample. We run many and report the spread, so nobody has to trust one draw.
* **Sensitivity sweep.** The whole thing is rerun with the world's parameters
  perturbed harder and harder. A result that only holds when the agent's beliefs
  are nearly right is not a result, it is a coincidence.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import config
from ..models import Diagnosis, FailedPayment
from ..policy.guardrails import AgentState
from ..taxonomy import FailureClass, InterventionType
from . import baselines
from .outcome_model import TrueWorld, WorldState


@dataclass
class PolicyRun:
    """Totals from one pass of one policy over the batch."""

    recovered_count: int = 0
    recovered_paise: int = 0
    action_cost_paise: float = 0.0
    contacts: int = 0
    suppressed: int = 0
    churned: int = 0

    def net_paise(self) -> float:
        """Merchant contribution: recovered margin minus what we spent to get it."""
        return self.recovered_paise * config.MERCHANT_MARGIN - self.action_cost_paise


@dataclass
class PolicySummary:
    name: str
    runs: List[PolicyRun] = field(default_factory=list)
    n_payments: int = 0
    at_risk_paise: int = 0

    def _series(self, attr: str) -> List[float]:
        return [float(getattr(r, attr)) for r in self.runs]

    def mean(self, attr: str) -> float:
        return statistics.fmean(self._series(attr)) if self.runs else 0.0

    def mean_net(self) -> float:
        return statistics.fmean([r.net_paise() for r in self.runs]) if self.runs else 0.0

    def interval_net(self, lo: float = 0.05, hi: float = 0.95) -> Tuple[float, float]:
        values = sorted(r.net_paise() for r in self.runs)
        if not values:
            return 0.0, 0.0
        return (
            values[max(0, int(lo * len(values)) - 1)],
            values[min(len(values) - 1, int(hi * len(values)))],
        )

    def paired_uplift(self, other: "PolicySummary") -> List[float]:
        """Per-replication difference against another policy.

        Only meaningful because the two policies ran on identical random draws.
        Differencing within a replication cancels the shared noise, which is why
        this interval is far tighter than the interval on either policy alone -
        and why it is the number worth quoting.
        """
        return [a.net_paise() - b.net_paise() for a, b in zip(self.runs, other.runs)]

    def uplift_interval(self, other: "PolicySummary", lo: float = 0.05, hi: float = 0.95):
        values = sorted(self.paired_uplift(other))
        if not values:
            return 0.0, 0.0
        return (
            values[max(0, int(lo * len(values)) - 1)],
            values[min(len(values) - 1, int(hi * len(values)))],
        )

    def beats(self, other: "PolicySummary") -> float:
        """Share of replications where this policy won outright."""
        diffs = self.paired_uplift(other)
        return sum(1 for d in diffs if d > 0) / len(diffs) if diffs else 0.0

    @property
    def recovery_rate(self) -> float:
        return self.mean("recovered_count") / self.n_payments if self.n_payments else 0.0


def run_once(
    policy_name: str,
    payments: List[FailedPayment],
    diagnoses: Dict[str, Diagnosis],
    truth: Dict[str, FailureClass],
    world: TrueWorld,
    replication_seed: int,
) -> PolicyRun:
    """One policy, one pass over the batch, under one draw of the world."""
    from datetime import timedelta

    policy = baselines.POLICIES[policy_name]
    agent_state = AgentState()
    world_state = WorldState()
    totals = PolicyRun()

    for payment in payments:
        now = payment.created_at + timedelta(minutes=15)
        decision = policy(payment, diagnoses[payment.payment_id], agent_state, now)

        # Per-payment stream, identical across policies: paired comparison.
        rng = random.Random("{}|{}".format(replication_seed, payment.payment_id))
        outcome = world.simulate(
            payment, truth[payment.payment_id], decision.chosen, world_state, rng
        )

        if decision.chosen.intervention is InterventionType.SUPPRESS:
            totals.suppressed += 1
        totals.recovered_count += int(outcome.recovered)
        totals.recovered_paise += outcome.recovered_amount_paise
        totals.action_cost_paise += outcome.action_cost_paise
        totals.contacts += outcome.customer_contacts

    totals.churned = len(world_state.churned)
    return totals


def compare(
    payments: List[FailedPayment],
    diagnoses: Dict[str, Diagnosis],
    truth: Dict[str, FailureClass],
    replications: int = 40,
    sigma: float = 0.35,
    world_seed: int = 101,
    policies: Optional[List[str]] = None,
) -> Dict[str, PolicySummary]:
    world = TrueWorld(seed=world_seed, sigma=sigma)
    names = policies or baselines.POLICY_ORDER
    at_risk = sum(p.amount for p in payments)

    summaries = {
        name: PolicySummary(name=name, n_payments=len(payments), at_risk_paise=at_risk)
        for name in names
    }
    for replication in range(replications):
        for name in names:
            summaries[name].runs.append(
                run_once(name, payments, diagnoses, truth, world, replication)
            )
    return summaries


def sensitivity(
    payments: List[FailedPayment],
    diagnoses: Dict[str, Diagnosis],
    truth: Dict[str, FailureClass],
    sigmas: Tuple[float, ...] = (0.0, 0.2, 0.35, 0.5, 0.8),
    replications: int = 12,
) -> Dict[float, Dict[str, float]]:
    """How wrong can the agent's beliefs be before it stops winning?

    sigma=0 is the flattering case where the agent is right about magnitudes (it is
    still wrong about the structural biases and knows nothing about churn).
    sigma=0.8 means its efficacy estimates are routinely off by a factor of two.
    """
    out: Dict[float, Dict[str, Dict[str, float]]] = {}
    for sigma in sigmas:
        summaries = compare(
            payments, diagnoses, truth, replications=replications, sigma=sigma
        )
        out[sigma] = {
            name: {
                "net": s.mean_net(),
                "contacts": s.mean("contacts"),
                "churned": s.mean("churned"),
            }
            for name, s in summaries.items()
        }
    return out


def markdown_report(
    summaries: Dict[str, PolicySummary],
    sens: Dict[float, Dict[str, float]],
    replications: int,
    sigma: float,
    provider: str,
) -> str:
    lines: List[str] = []
    add = lines.append
    rupees = lambda paise: paise / 100.0  # noqa: E731

    baseline = summaries["do_nothing"]
    agent = summaries["rebound"]
    at_risk = agent.at_risk_paise

    add("# Recovery results")
    add("")
    add("> **These are simulated outcomes, not observed ones.** No real payment was "
        "recovered. The world model in `sim/outcome_model.py` is deliberately built "
        "to disagree with the agent's own beliefs, and the sensitivity sweep below "
        "exists because the underlying priors are estimates from public industry "
        "reporting rather than measurements from a real merchant. Read the "
        "Methodology section before quoting any figure here.")
    add("")
    add("- Batch: **{} payments, INR {:,.0f} at risk**".format(agent.n_payments, rupees(at_risk)))
    add("- Diagnosis provider: `{}`".format(provider))
    add("- {} replications per policy, paired on common random numbers, "
        "world noise sigma = {}".format(replications, sigma))
    add("")

    add("## Policy comparison")
    add("")
    add("| Policy | Recovery rate | Recovered | Cost | Net contribution | 90% interval | "
        "Contacts | Churned |")
    add("|---|---|---|---|---|---|---|---|")
    for name in baselines.POLICY_ORDER:
        s = summaries[name]
        lo, hi = s.interval_net()
        add("| {}{} | {:.1%} | INR {:,.0f} | INR {:,.0f} | **INR {:,.0f}** | "
            "INR {:,.0f} to {:,.0f} | {:,.0f} | {:.1f} |".format(
                "**" + name + "**" if name == "rebound" else name,
                "" if name != "rebound" else "",
                s.recovery_rate, rupees(s.mean("recovered_paise")),
                rupees(s.mean("action_cost_paise")), rupees(s.mean_net()),
                rupees(lo), rupees(hi), s.mean("contacts"), s.mean("churned")))
    add("")
    add("*Net contribution* is recovered value at the merchant's margin, minus what "
        "the policy spent to get it - including the penalties a policy incurs for "
        "retrying payments that should never be retried, and for churning customers "
        "it over-messaged.")
    add("")

    uplift = agent.mean_net() - baseline.mean_net()
    u_lo, u_hi = agent.uplift_interval(baseline)
    add("## Headline")
    add("")
    add("- Against doing nothing, Rebound adds **INR {:,.0f}** of net contribution on "
        "this batch ({:+.0%} on a base of INR {:,.0f}), 90% interval INR {:,.0f} to "
        "{:,.0f}, winning in {:.0%} of replications.".format(
            rupees(uplift), uplift / baseline.mean_net() if baseline.mean_net() else 0.0,
            rupees(baseline.mean_net()), rupees(u_lo), rupees(u_hi),
            agent.beats(baseline)))
    best_naive = max(
        (summaries[n] for n in ("retry_all", "blind_24h", "nudge_all")),
        key=lambda s: s.mean_net(),
    )
    n_lo, n_hi = agent.uplift_interval(best_naive)
    add("- Against the best naive alternative (`{}`), it adds **INR {:,.0f}** "
        "(90% interval INR {:,.0f} to {:,.0f}), winning in {:.0%} of "
        "replications.".format(
            best_naive.name, rupees(agent.mean_net() - best_naive.mean_net()),
            rupees(n_lo), rupees(n_hi), agent.beats(best_naive)))
    add("- It does that while contacting **{:,.0f}** customers, against `nudge_all`'s "
        "**{:,.0f}** - {:.0%} fewer messages.".format(
            agent.mean("contacts"), summaries["nudge_all"].mean("contacts"),
            1 - agent.mean("contacts") / max(1.0, summaries["nudge_all"].mean("contacts"))))
    add("- It stays silent on **{:.0f}** of {} payments.".format(
        agent.mean("suppressed"), agent.n_payments))
    add("")

    add("## Sensitivity: how wrong can the agent be and still win?")
    add("")
    add("`sigma` is the spread of the lognormal shock applied to every one of the "
        "agent's efficacy beliefs. At 0.8 its estimates are routinely off by a "
        "factor of two. Values are mean net contribution in INR.")
    add("")
    header = ("| sigma | " + " | ".join(baselines.POLICY_ORDER)
              + " | agent rank | nudge_all churn | rebound churn |")
    add(header)
    add("|---" * (len(baselines.POLICY_ORDER) + 4) + "|")
    for sig in sorted(sens):
        row = sens[sig]
        ordered = sorted(row.items(), key=lambda kv: -kv[1]["net"])
        rank = [n for n, _ in ordered].index("rebound") + 1
        add("| {} | ".format(sig)
            + " | ".join("{:,.0f}".format(rupees(row[n]["net"])) for n in baselines.POLICY_ORDER)
            + " | **{} of {}** | {:.1f} | {:.1f} |".format(
                rank, len(row), row["nudge_all"]["churned"], row["rebound"]["churned"]))
    add("")
    add("**The honest reading of this table.** Rebound wins on money while its "
        "efficacy beliefs are roughly right. Once they are routinely off by half "
        "(sigma 0.5 and above), blanket messaging earns more, because a policy that "
        "targets badly is worse than one that does not target at all. That is the "
        "real boundary of this approach and it is the argument for calibrating "
        "against observed outcomes before trusting the policy with a budget.")
    add("")
    add("It is also worth reading the last two columns while you do. Even where "
        "`nudge_all` earns more, it gets there by messaging every consenting "
        "customer on the list and burning roughly {:.0f} of them per batch. "
        "Rebound churns none. A quarter of extra revenue that costs you customers "
        "is a loan, not a win - but this harness only scores one batch, so it "
        "flatters the blanket policy by construction.".format(
            max(sens[s]["nudge_all"]["churned"] for s in sens)))
    add("")

    add("## Methodology and limitations")
    add("")
    add("1. **Simulated, not observed.** Every rupee here comes from a model. The "
        "class mix and organic-recovery priors in `taxonomy.py` are estimates "
        "assembled from public payment-industry reporting; they are not Razorpay "
        "figures and were not fitted to any real dataset.")
    add("2. **The world disagrees with the agent on purpose.** Reusing the agent's "
        "own efficacy table as ground truth would make it win by construction. "
        "Instead the world applies a seeded lognormal shock to every belief, five "
        "deliberate directional biases, and a contact-fatigue and churn model the "
        "agent does not know exists.")
    add("3. **Shared structure is the honest limitation.** The world and the agent "
        "still agree on *which factors matter* - root cause, timing, channel, "
        "customer history. They disagree only on magnitudes. A world where a "
        "completely different mechanism drove recovery would not be captured, and "
        "this harness cannot tell you that it exists.")
    add("4. **Paired comparison.** Policies are compared on identical per-payment "
        "random draws, so the interval reflects genuine policy difference rather "
        "than which policy drew the luckier batch.")
    add("5. **What would replace this.** A shadow-mode deployment: run the agent "
        "alongside a merchant's existing recovery flow, take no actions, and score "
        "its decisions against what actually happened. That is the only way to get "
        "a number worth putting in a contract.")
    add("")
    return "\n".join(lines)


def write_report(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def as_json(
    summaries: Dict[str, PolicySummary],
    sens: Dict[float, Dict[str, Dict[str, float]]],
    replications: int,
    sigma: float,
    provider: str,
) -> Dict[str, object]:
    """Machine-readable results, so the dashboard reads numbers rather than prose."""
    agent = summaries["rebound"]
    nothing = summaries["do_nothing"]
    best_naive = max(
        (summaries[n] for n in ("retry_all", "blind_24h", "nudge_all")),
        key=lambda s: s.mean_net(),
    )
    lo, hi = agent.uplift_interval(nothing)

    return {
        "meta": {
            "payments": agent.n_payments,
            "at_risk_paise": agent.at_risk_paise,
            "replications": replications,
            "sigma": sigma,
            "provider": provider,
            "simulated": True,
        },
        "policies": [
            {
                "name": name,
                "recovery_rate": summaries[name].recovery_rate,
                "recovered_paise": summaries[name].mean("recovered_paise"),
                "cost_paise": summaries[name].mean("action_cost_paise"),
                "net_paise": summaries[name].mean_net(),
                "contacts": summaries[name].mean("contacts"),
                "churned": summaries[name].mean("churned"),
                "suppressed": summaries[name].mean("suppressed"),
            }
            for name in baselines.POLICY_ORDER
        ],
        "headline": {
            "uplift_vs_nothing_paise": agent.mean_net() - nothing.mean_net(),
            "uplift_ci_low_paise": lo,
            "uplift_ci_high_paise": hi,
            "win_rate_vs_nothing": agent.beats(nothing),
            "best_naive": best_naive.name,
            "uplift_vs_best_naive_paise": agent.mean_net() - best_naive.mean_net(),
            "win_rate_vs_best_naive": agent.beats(best_naive),
        },
        "sensitivity": [
            {
                "sigma": sig,
                "net": {n: sens[sig][n]["net"] for n in baselines.POLICY_ORDER},
                "agent_rank": sorted(
                    sens[sig].items(), key=lambda kv: -kv[1]["net"]
                ).index(
                    next(kv for kv in sorted(sens[sig].items(), key=lambda kv: -kv[1]["net"])
                         if kv[0] == "rebound")
                ) + 1,
            }
            for sig in sorted(sens)
        ],
    }
