"""Systemic findings: stop chasing payments one at a time and fix the cause.

Per-payment recovery is the feature. Noticing that 40% of a merchant's failures
share one root cause, in one time window, on one rail - and telling them to change
that - is the product. A merchant would rather hear "move your subscription debits
from 2am to 11am" once than watch an agent heroically chase the same preventable
failure every night.

Detection here is entirely deterministic: grouping, sorting, thresholds. No model
is involved in finding a pattern, because a model that hallucinates a business
recommendation with a rupee figure attached is worse than no recommendation. The
optional LLM pass in `narrate()` only rewrites findings we already computed, and
it can be skipped with no loss of substance.

Every finding carries the money at stake, so a merchant can rank them.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from ..models import FailedPayment
from ..taxonomy import NEVER_CONTACT, NEVER_RETRY, FailureClass, profile

# A finding has to clear both bars to be worth a merchant's attention: enough
# money, and enough concentration to be a pattern rather than noise.
MIN_SHARE = 0.12
MIN_VALUE_PAISE = 50_000


@dataclass
class Insight:
    title: str
    detail: str
    recommendation: str
    value_paise: float
    share: float
    severity: str = "medium"          # high | medium | low
    evidence: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "detail": self.detail,
            "recommendation": self.recommendation,
            "value_paise": round(self.value_paise, 2),
            "value_rupees": round(self.value_paise / 100, 2),
            "share": round(self.share, 4),
            "severity": self.severity,
            "evidence": self.evidence,
        }


def _rupees(paise: float) -> str:
    return "INR {:,.0f}".format(paise / 100)


def _by(payments: Sequence[FailedPayment], key) -> Dict[Any, List[FailedPayment]]:
    out: Dict[Any, List[FailedPayment]] = defaultdict(list)
    for p in payments:
        out[key(p)].append(p)
    return out


def _value(payments: Sequence[FailedPayment]) -> int:
    return sum(p.amount for p in payments)


def find_downtime_windows(
    payments: Sequence[FailedPayment], truth: Dict[str, FailureClass]
) -> List[Insight]:
    """Outages cluster in time. A merchant can simply stop debiting during them."""
    downtime = [p for p in payments if truth.get(p.payment_id) is FailureClass.BANK_DOWNTIME]
    if not downtime:
        return []

    total = _value(downtime)
    by_hour = _by(downtime, lambda p: p.created_at.hour)
    hourly = {h: _value(ps) for h, ps in by_hour.items()}

    # Best contiguous three-hour window, wrapping past midnight.
    best_start, best_value = 0, 0
    for start in range(24):
        window = sum(hourly.get((start + offset) % 24, 0) for offset in range(3))
        if window > best_value:
            best_start, best_value = start, window

    share = best_value / total if total else 0.0
    if share < 0.25 or best_value < MIN_VALUE_PAISE:
        return []

    end = (best_start + 3) % 24
    return [Insight(
        title="Bank downtime clusters between {:02d}:00 and {:02d}:00".format(best_start, end),
        detail=(
            "{} of all downtime-related failure value lands in a three-hour window. "
            "That is an issuer availability pattern, not customer behaviour - the same "
            "payments would very likely succeed a few hours later."
        ).format("{:.0%}".format(share)),
        recommendation=(
            "Move scheduled debits, subscription renewals and mandate presentments out "
            "of {:02d}:00-{:02d}:00. For anything that must run then, set the first retry "
            "to land after {:02d}:00 rather than the default short backoff."
        ).format(best_start, end, end),
        value_paise=best_value,
        share=share,
        severity="high",
        evidence={"window_start": best_start, "window_end": end,
                  "hourly_value_paise": hourly},
    )]


def find_class_concentration(
    payments: Sequence[FailedPayment], truth: Dict[str, FailureClass]
) -> List[Insight]:
    """One root cause usually dominates. Naming it is half the fix."""
    total = _value(payments)
    grouped = _by(payments, lambda p: truth.get(p.payment_id, FailureClass.UNKNOWN))
    insights: List[Insight] = []

    fixes = {
        FailureClass.AUTH_DROPOFF:
            "Checkout drop-off is a UX problem before it is a recovery problem. Shorten "
            "the OTP step, keep the customer on-page, and pre-fill the instrument.",
        FailureClass.INSUFFICIENT_FUNDS:
            "Align retries with salary cycles rather than a fixed backoff, and offer a "
            "smaller first instalment where the ticket allows it.",
        FailureClass.EXPIRED_INSTRUMENT:
            "Enable card-updater or tokenised network updates so expiries are refreshed "
            "before they cause a failure at all.",
        FailureClass.MANDATE_INACTIVE:
            "Run a re-mandate campaign ahead of renewal dates instead of discovering "
            "revoked authority at charge time.",
        FailureClass.LIMIT_EXCEEDED:
            "Surface an alternative rail at checkout for tickets above the common "
            "per-transaction cap.",
    }

    for failure_class, group in sorted(grouped.items(), key=lambda kv: -_value(kv[1])):
        value = _value(group)
        share = value / total if total else 0.0
        if share < MIN_SHARE or value < MIN_VALUE_PAISE:
            continue
        recoverable = profile(failure_class).base_recovery
        insights.append(Insight(
            title="{} is {:.0%} of failed value".format(failure_class.value, share),
            detail=(
                "{} payments worth {}. Left alone, roughly {:.0%} of these recover on "
                "their own, which puts about {} genuinely at risk."
            ).format(len(group), _rupees(value), recoverable,
                     _rupees(value * (1 - recoverable))),
            recommendation=fixes.get(
                failure_class,
                "Investigate the upstream cause before scaling recovery spend against it.",
            ),
            value_paise=value,
            share=share,
            severity="high" if share > 0.2 else "medium",
            evidence={"count": len(group), "class": failure_class.value},
        ))
    return insights[:3]


def find_unrecoverable(
    payments: Sequence[FailedPayment], truth: Dict[str, FailureClass]
) -> List[Insight]:
    """Money it would be a mistake to chase. Saying so is also advice."""
    dead = [p for p in payments
            if truth.get(p.payment_id) in (NEVER_RETRY | NEVER_CONTACT)
            and truth.get(p.payment_id) in (FailureClass.SUSPECTED_FRAUD,
                                            FailureClass.RISK_DECLINE_ISSUER,
                                            FailureClass.CUSTOMER_CANCELLED)]
    if not dead:
        return []
    total = _value(payments)
    value = _value(dead)
    share = value / total if total else 0.0
    if value < MIN_VALUE_PAISE:
        return []

    return [Insight(
        title="{} of failed value should not be chased at all".format("{:.0%}".format(share)),
        detail=(
            "{} payments worth {} were declined for fraud, issuer risk, or deliberate "
            "customer cancellation. Retrying these earns fees and chargeback exposure "
            "rather than revenue, and messaging the customer is worse than silence."
        ).format(len(dead), _rupees(value)),
        recommendation=(
            "Exclude these classes from any recovery campaign, and measure your recovery "
            "rate against the addressable base rather than all failures - otherwise the "
            "ceiling looks lower than it is and every tool underperforms on paper."
        ),
        value_paise=value,
        share=share,
        severity="medium",
        evidence={"count": len(dead)},
    )]


def find_rail_weakness(
    payments: Sequence[FailedPayment], truth: Dict[str, FailureClass]
) -> List[Insight]:
    """Which rail is costing the most, and is it a routing problem."""
    total = _value(payments)
    grouped = _by(payments, lambda p: p.rail)
    ranked = sorted(grouped.items(), key=lambda kv: -_value(kv[1]))
    if not ranked:
        return []

    rail, group = ranked[0]
    value = _value(group)
    share = value / total if total else 0.0
    if share < MIN_SHARE:
        return []

    classes = _by(group, lambda p: truth.get(p.payment_id, FailureClass.UNKNOWN))
    top_class, top_group = max(classes.items(), key=lambda kv: _value(kv[1]))
    return [Insight(
        title="{} carries {:.0%} of failed value".format(rail.value, share),
        detail=(
            "{} of failures on this rail, worth {}, and its single largest cause is "
            "{} ({} of the rail's failed value)."
        ).format(len(group), _rupees(value), top_class.value,
                 "{:.0%}".format(_value(top_group) / value if value else 0)),
        recommendation=(
            "Compare authorisation rates across your acquirers on this rail before "
            "investing further in recovery. A routing change fixes failures that "
            "recovery can only chase after the fact."
        ),
        value_paise=value,
        share=share,
        severity="medium",
        evidence={"rail": rail.value, "top_cause": top_class.value},
    )]


def find_repeat_customers(payments: Sequence[FailedPayment]) -> List[Insight]:
    """Customers failing repeatedly are a different problem from one-off failures.

    The threshold is set against the batch's own base rate rather than a fixed
    number. In a batch where the average customer appears three times, "failed
    three or more times" describes almost everybody and is a property of the
    dataset, not a finding. Anything flagged here has to be clearly above what
    this batch produces by default.
    """
    grouped = _by(payments, lambda p: p.customer_id)
    if not grouped:
        return []

    mean_per_customer = len(payments) / len(grouped)
    threshold = max(3, int(mean_per_customer * 2) + 1)

    repeats = {c: ps for c, ps in grouped.items() if len(ps) >= threshold}
    if not repeats:
        return []
    value = sum(_value(ps) for ps in repeats.values())
    total = _value(payments)
    if value < MIN_VALUE_PAISE or len(repeats) / len(grouped) > 0.25:
        # More than a quarter of the base is not a segment, it is the base.
        return []

    return [Insight(
        title="{} customers failed {} or more times".format(len(repeats), threshold),
        detail=(
            "They account for {} of failed value, against a batch average of {:.1f} "
            "failures per customer. Repeat failure by the same payer is usually a "
            "broken saved instrument or a persistent issuer block, not bad luck."
        ).format(_rupees(value), mean_per_customer),
        recommendation=(
            "Route these to a one-time human or assisted flow rather than another "
            "automated attempt. Each additional silent retry lowers the odds and, on a "
            "contact channel, raises churn risk."
        ),
        value_paise=value,
        share=value / total if total else 0.0,
        severity="medium",
        evidence={"customers": len(repeats)},
    )]


def analyse(
    payments: Sequence[FailedPayment], truth: Dict[str, FailureClass]
) -> List[Insight]:
    """All detectors, ranked by money at stake."""
    found: List[Insight] = []
    found += find_downtime_windows(payments, truth)
    found += find_class_concentration(payments, truth)
    found += find_rail_weakness(payments, truth)
    found += find_unrecoverable(payments, truth)
    found += find_repeat_customers(payments)
    return sorted(found, key=lambda i: -i.value_paise)


def markdown(insights: Sequence[Insight], payments: Sequence[FailedPayment]) -> str:
    total = _value(payments)
    lines = [
        "# What to fix, not just what to chase",
        "",
        "Per-payment recovery treats symptoms. These are the patterns underneath, "
        "ranked by the money involved.",
        "",
        "Batch: **{} failed payments, {} at risk**.".format(len(payments), _rupees(total)),
        "",
        "Findings are computed by deterministic grouping and thresholds in "
        "`analytics/insights.py` - no model invents a recommendation or a number.",
        "",
    ]
    for i, insight in enumerate(insights, 1):
        lines += [
            "## {}. {}".format(i, insight.title),
            "",
            "**{}** at stake — {:.0%} of failed value · severity {}".format(
                _rupees(insight.value_paise), insight.share, insight.severity),
            "",
            insight.detail,
            "",
            "> **Do this:** {}".format(insight.recommendation),
            "",
        ]
    return "\n".join(lines)
