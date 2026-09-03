"""Learning the efficacy numbers instead of asserting them.

The sensitivity sweep in `reports/recovery.md` is the most uncomfortable result in
this project: once the agent's efficacy beliefs are routinely wrong by half, it
stops beating blanket messaging, because a policy that targets badly is worse than
one that does not target at all.

Those beliefs are currently hand-written constants in `economics.py`. This module
is the mechanism that replaces them with evidence.

**Beta-Bernoulli posteriors.** Each (root cause, action) pair gets a Beta
posterior over its recovery probability, seeded from the hand-written prior with a
deliberately weak pseudo-count. Early on the agent behaves exactly as before;
after a few hundred observed outcomes the data dominates the guess. Nothing has to
be retuned by hand, and the prior's influence decays on its own.

**Thompson sampling, bounded by ticket value.** An agent that always takes the
action it currently believes is best can never discover that it is wrong - it has
no counterfactual. So instead of using the posterior mean, we draw from the
posterior: uncertain arms sometimes win, get tried, and get evidence. Exploration
shrinks by itself as confidence grows, with no epsilon to tune.

The bound is the part worth arguing for: **exploration is only permitted below a
ticket ceiling.** Learning has a price, and it should be paid with small payments,
not large ones. On anything valuable the agent exploits its best current estimate.
That keeps the cost of curiosity proportional to its value.

Every guardrail still applies. Exploration widens which *permitted* action gets
chosen; it can never reach a forbidden one.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from .. import config
from ..taxonomy import FailureClass, InterventionType
from . import economics

# How much the hand-written prior is worth in imaginary observations. Low on
# purpose: these numbers are estimates from public reporting, not measurements, so
# roughly a dozen real outcomes should be enough to start overruling them.
PRIOR_STRENGTH = 12.0

# Above this ticket, always exploit. Learning is worth paying for with small
# payments, not with a merchant's biggest orders.
EXPLORE_CEILING_PAISE = int(
    __import__("os").environ.get("REBOUND_EXPLORE_CEILING_PAISE", "200000")
)

Key = Tuple[FailureClass, InterventionType]


@dataclass
class Posterior:
    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def observations(self) -> float:
        """Real evidence accumulated, excluding the prior's pseudo-count."""
        return max(0.0, self.alpha + self.beta - PRIOR_STRENGTH)

    @property
    def stderr(self) -> float:
        n = self.alpha + self.beta
        return math.sqrt(self.mean * (1 - self.mean) / n) if n else 0.5

    def sample(self, rng: random.Random) -> float:
        return rng.betavariate(max(1e-6, self.alpha), max(1e-6, self.beta))


class Calibrator:
    """Posterior beliefs about how well each action works for each root cause."""

    def __init__(self, explore: bool = True, seed: int = 0):
        self.explore = explore
        self.rng = random.Random(seed)
        self.posteriors: Dict[Key, Posterior] = {}
        for key, prior in economics.ACTION_EFFICACY.items():
            prior = min(0.95, max(0.01, prior))
            self.posteriors[key] = Posterior(
                alpha=prior * PRIOR_STRENGTH,
                beta=(1 - prior) * PRIOR_STRENGTH,
            )

    # ------------------------------------------------------------------ learning
    def update(
        self, failure_class: FailureClass, intervention: InterventionType, recovered: bool
    ) -> None:
        key = (failure_class, intervention)
        post = self.posteriors.get(key)
        if post is None:
            return
        if recovered:
            post.alpha += 1.0
        else:
            post.beta += 1.0

    def update_many(self, observations: Iterable[Tuple[FailureClass, InterventionType, bool]]) -> int:
        n = 0
        for failure_class, intervention, recovered in observations:
            self.update(failure_class, intervention, recovered)
            n += 1
        return n

    # ------------------------------------------------------------------ reading
    def efficacy(
        self,
        failure_class: FailureClass,
        intervention: InterventionType,
        amount_paise: int = 0,
    ) -> float:
        """The number the policy should use for this decision.

        Thompson sample below the exploration ceiling, posterior mean above it.
        """
        post = self.posteriors.get((failure_class, intervention))
        if post is None:
            return 0.0
        if self.explore and amount_paise <= EXPLORE_CEILING_PAISE:
            return post.sample(self.rng)
        return post.mean

    def drift(self) -> Dict[str, Dict[str, float]]:
        """How far each belief has moved from the hand-written prior, and on what."""
        out: Dict[str, Dict[str, float]] = {}
        for (failure_class, intervention), post in self.posteriors.items():
            prior = economics.ACTION_EFFICACY[(failure_class, intervention)]
            if post.observations < 1:
                continue
            out["{}|{}".format(failure_class.value, intervention.value)] = {
                "prior": round(prior, 3),
                "posterior": round(post.mean, 3),
                "shift": round(post.mean - prior, 3),
                "observations": round(post.observations, 1),
                "stderr": round(post.stderr, 3),
            }
        return dict(sorted(out.items(), key=lambda kv: -abs(kv[1]["shift"])))

    # --------------------------------------------------------------- persistence
    def save(self, ledger) -> None:
        ledger.conn.execute(
            "CREATE TABLE IF NOT EXISTS calibration ("
            "  failure_class TEXT NOT NULL, intervention TEXT NOT NULL,"
            "  alpha REAL NOT NULL, beta REAL NOT NULL,"
            "  PRIMARY KEY (failure_class, intervention))"
        )
        ledger.conn.executemany(
            "INSERT OR REPLACE INTO calibration VALUES (?,?,?,?)",
            [(fc.value, iv.value, p.alpha, p.beta)
             for (fc, iv), p in self.posteriors.items()],
        )
        ledger.conn.commit()

    def load(self, ledger) -> int:
        try:
            rows = ledger.query("SELECT * FROM calibration")
        except Exception:  # noqa: BLE001 - table absent on a fresh database
            return 0
        for row in rows:
            key = (FailureClass(row["failure_class"]), InterventionType(row["intervention"]))
            if key in self.posteriors:
                self.posteriors[key] = Posterior(alpha=row["alpha"], beta=row["beta"])
        return len(rows)


def observations_from_ledger(ledger, run_id: Optional[str] = None):
    """Every (cause, action, recovered) triple the ledger can testify to.

    Joins decisions to outcomes, so only payments the agent actually acted on and
    whose fate is known contribute. Suppressions are excluded: doing nothing
    teaches you nothing about how well an action works.
    """
    sql = (
        "SELECT d.failure_class, d.intervention, o.recovered "
        "FROM decisions d JOIN outcomes o ON o.payment_id = d.payment_id "
        "WHERE d.intervention NOT IN ('suppress', 'escalate_human')"
    )
    params: tuple = ()
    if run_id:
        sql += " AND d.run_id = ?"
        params = (run_id,)

    for row in ledger.query(sql, params):
        try:
            yield (FailureClass(row["failure_class"]),
                   InterventionType(row["intervention"]),
                   bool(row["recovered"]))
        except ValueError:
            continue
