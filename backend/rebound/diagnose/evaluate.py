"""Scores the classifier against held-out ground truth and writes a report.

Produces four things the pitch depends on:

  1. per-class precision / recall / F1 with support
  2. the confusion matrix
  3. rupee cost of classifier errors (see error_cost.py)
  4. an ablation: rules-only vs model-only vs hybrid, on accuracy AND cost AND time

The ablation is the point. It is what turns "we used AI responsibly" from a claim
into a measurement.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import config
from ..models import Diagnosis, FailedPayment
from ..taxonomy import FailureClass
from .classifier import HybridClassifier
from .error_cost import error_cost_paise, worst_confusions
from .llm import OfflineProvider, TailClassifier


@dataclass
class ClassMetrics:
    label: str
    support: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class ClassifierReport:
    name: str
    n: int = 0
    correct: int = 0
    abstained: int = 0
    per_class: Dict[str, ClassMetrics] = field(default_factory=dict)
    confusion: Dict[Tuple[str, str], int] = field(default_factory=dict)
    error_cost_paise: float = 0.0
    elapsed_s: float = 0.0
    model_rows: int = 0        # rows routed to the model
    llm_calls: int = 0         # network calls actually made (cache misses only)
    llm_cost_usd: float = 0.0
    degraded_rows: int = 0   # rows the provider refused, served by offline fallback
    deterministic_share: float = 0.0
    demoted_low_confidence: int = 0
    demoted_hostile: int = 0
    provider: str = ""

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0

    @property
    def coverage(self) -> float:
        """Share of rows given a real class rather than UNKNOWN."""
        return (self.n - self.abstained) / self.n if self.n else 0.0

    @property
    def accuracy_when_answered(self) -> float:
        answered = self.n - self.abstained
        return self.correct / answered if answered else 0.0

    @property
    def macro_f1(self) -> float:
        scored = [m for m in self.per_class.values() if m.support]
        return sum(m.f1 for m in scored) / len(scored) if scored else 0.0


def score(
    name: str,
    payments: List[FailedPayment],
    labels: Dict[str, FailureClass],
    diagnoses: List[Diagnosis],
    elapsed_s: float,
) -> ClassifierReport:
    report = ClassifierReport(name=name, n=len(payments), elapsed_s=elapsed_s)

    for failure_class in FailureClass:
        report.per_class[failure_class.value] = ClassMetrics(label=failure_class.value)

    by_id = {d.payment_id: d for d in diagnoses}
    for payment in payments:
        truth = labels[payment.payment_id]
        predicted = by_id[payment.payment_id].failure_class

        report.per_class[truth.value].support += 1
        key = (truth.value, predicted.value)
        report.confusion[key] = report.confusion.get(key, 0) + 1

        if predicted is FailureClass.UNKNOWN and truth is not FailureClass.UNKNOWN:
            report.abstained += 1

        if predicted is truth:
            report.correct += 1
            report.per_class[truth.value].tp += 1
        else:
            report.per_class[truth.value].fn += 1
            report.per_class[predicted.value].fp += 1
            report.error_cost_paise += error_cost_paise(truth, predicted, payment.amount)

    return report


def run_variant(
    name: str,
    payments: List[FailedPayment],
    labels: Dict[str, FailureClass],
    use_model: bool,
    force_all_to_model: bool = False,
    provider_override: Optional[str] = None,
) -> ClassifierReport:
    """Runs one arm of the ablation over the full batch."""
    tail = TailClassifier(OfflineProvider()) if provider_override == "offline" else TailClassifier()
    classifier = HybridClassifier(tail=tail, use_model=use_model)

    def diagnose_one(payment: FailedPayment) -> Diagnosis:
        if not force_all_to_model:
            return classifier.diagnose(payment)
        # Model-only arm: skip the rules entirely so we can measure what the
        # model would have done on traffic the rules handle for free.
        from .sanitize import sanitize_untrusted

        clean, flags = sanitize_untrusted(payment.error_description or "")
        diagnosis = classifier.tail.diagnose(payment, clean, flags)
        diagnosis.flags = flags
        return classifier._apply_safety_floor(diagnosis, flags)

    started = time.perf_counter()
    # Network-backed providers are latency-bound, so fan out. The offline provider
    # is pure CPU and threads would only add contention, so it stays serial.
    workers = config.LLM_WORKERS if (use_model and classifier.tail.provider.is_language_model) else 1
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            diagnoses = list(pool.map(diagnose_one, payments))
    else:
        diagnoses = [diagnose_one(p) for p in payments]
    elapsed = time.perf_counter() - started

    report = score(name, payments, labels, diagnoses, elapsed)
    report.model_rows = len(payments) if force_all_to_model else classifier.stats.by_model
    report.llm_calls = classifier.tail.call_count
    report.llm_cost_usd = classifier.tail.total_cost_usd
    report.degraded_rows = classifier.tail.degraded_count
    report.deterministic_share = 0.0 if force_all_to_model else classifier.stats.deterministic_share
    report.demoted_low_confidence = classifier.stats.demoted_low_confidence
    report.demoted_hostile = classifier.stats.demoted_hostile
    report.provider = classifier.tail.provider.name
    return report


def markdown_report(
    reports: List[ClassifierReport],
    batch_at_risk_paise: int,
    drift_reports: Optional[Dict[str, List[ClassifierReport]]] = None,
    notes: Optional[List[str]] = None,
) -> str:
    lines: List[str] = []
    add = lines.append

    add("# Classifier evaluation")
    add("")
    add("Scored against held-out labels the classifier never sees "
        "(`data/labels.json`, written by the generator and read only here).")
    add("")
    add("> **Read the drift section before trusting the headline table.** The main "
        "batch and the regex anchors in `rules.py` were written by the same author, "
        "so near-perfect rule precision on it is partly circular. The drift set "
        "exists to break that circularity and is the number we actually stand behind.")
    add("")

    if notes:
        add("### Coverage caps applied to this run")
        add("")
        for note in notes:
            add("- " + note)
        add("")

    add("## Ablation")
    add("")
    add("| Arm | Accuracy | Accuracy when answered | Coverage | Macro F1 | "
        "Error cost | Model rows | Net calls | LLM cost | Wall time |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for r in reports:
        add("| {name} | {acc:.1%} | {aca:.1%} | {cov:.1%} | {f1:.3f} | "
            "INR {cost:,.0f} | {rows} | {calls} | ${usd:.4f} | {secs:.2f}s |".format(
                name=r.name, acc=r.accuracy, aca=r.accuracy_when_answered,
                cov=r.coverage, f1=r.macro_f1, cost=r.error_cost_paise / 100,
                rows=r.model_rows, calls=r.llm_calls, usd=r.llm_cost_usd,
                secs=r.elapsed_s))
    add("")
    add("`Model rows` is how many payments were routed to the model. `Net calls` is "
        "how many of those actually hit the network - the rest were served from the "
        "on-disk response cache, which is why a re-run costs nothing.")
    add("")
    degraded = [r for r in reports if r.degraded_rows]
    if degraded:
        add("> **Degraded rows present.** " + "; ".join(
            "`{}` fell back to the offline classifier on {} of {} model rows".format(
                r.name, r.degraded_rows, r.model_rows) for r in degraded)
            + ". The provider refused those calls (free-tier quota), the circuit breaker "
              "opened, and the pipeline kept running on the offline classifier. Those "
              "arms are a blend of two classifiers and their accuracy should be read "
              "as a floor, not as the model's score.")
        add("")
    add("Error cost is the modelled rupee consequence of the mistakes each arm "
        "makes, not a count of them. See `backend/rebound/diagnose/error_cost.py` "
        "for every assumption behind it.")
    add("")

    if drift_reports:
        add("## Drift: held-out wording the rules were never tuned on")
        add("")
        add("`paraphrase` restates all 12 root causes using none of the anchor "
            "phrases in `rules.py`, with the structured reason code stripped. "
            "`noise` corrupts the original strings with typos, dropped words, "
            "upper-casing and truncation. Both keep the true labels.")
        add("")
        add("| Set | Arm | Accuracy | Coverage | Macro F1 | Error cost | Model rows |")
        add("|---|---|---|---|---|---|---|")
        for set_name, set_reports in drift_reports.items():
            for r in set_reports:
                add("| {} | {} | {:.1%} | {:.1%} | {:.3f} | INR {:,.0f} | {} |".format(
                    set_name, r.name, r.accuracy, r.coverage, r.macro_f1,
                    r.error_cost_paise / 100, r.model_rows))
        add("")
        add("This is where the model earns its place. Rule coverage collapses on "
            "unseen wording because high-precision anchors are, by construction, "
            "brittle to rewording - and the pipeline responds by routing far more "
            "rows to the model rather than guessing.")
        add("")

    hybrid = next((r for r in reports if r.name.startswith("hybrid")), reports[0])
    add("## Per-class detail - {}".format(hybrid.name))
    add("")
    add("| Class | Support | Precision | Recall | F1 |")
    add("|---|---|---|---|---|")
    for label, m in sorted(hybrid.per_class.items(), key=lambda kv: -kv[1].support):
        if not m.support and not m.fp:
            continue
        add("| {} | {} | {:.2f} | {:.2f} | {:.2f} |".format(
            label, m.support, m.precision, m.recall, m.f1))
    add("")

    add("## Confusion matrix - {}".format(hybrid.name))
    add("")
    predicted_labels = sorted({p for _, p in hybrid.confusion})
    add("| true \\ predicted | " + " | ".join(predicted_labels) + " |")
    add("|---" * (len(predicted_labels) + 1) + "|")
    true_labels = sorted({t for t, _ in hybrid.confusion})
    for t in true_labels:
        row = ["**" + t + "**"]
        for p in predicted_labels:
            count = hybrid.confusion.get((t, p), 0)
            row.append(str(count) if count else "")
        add("| " + " | ".join(row) + " |")
    add("")

    add("## Most expensive possible confusions")
    add("")
    add("At a reference ticket of INR 1,000. These drive what the test suite guards.")
    add("")
    add("| True | Predicted | Cost |")
    add("|---|---|---|")
    for t, p, c in worst_confusions(100_000):
        add("| {} | {} | INR {:,.0f} |".format(t, p, c / 100))
    add("")

    add("## Reading this")
    add("")
    add("- Batch at risk: INR {:,.0f} across {} payments.".format(
        batch_at_risk_paise / 100, hybrid.n))
    add("- Hybrid settles {:.0%} of rows with deterministic rules at zero marginal "
        "cost; the model is invoked only on the remainder.".format(hybrid.deterministic_share))
    add("- `Coverage` below 100% is deliberate. Rows below the confidence floor, and "
        "rows whose gateway text contained injection markers, are demoted to "
        "`unknown`, which the policy engine never retries and never contacts.")
    add("- Rows demoted this run: {} low-confidence, {} hostile.".format(
        hybrid.demoted_low_confidence, hybrid.demoted_hostile))
    add("")
    return "\n".join(lines)


def write_report(
    path: Path,
    reports: List[ClassifierReport],
    batch_at_risk_paise: int,
    drift_reports: Optional[Dict[str, List[ClassifierReport]]] = None,
    notes: Optional[List[str]] = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        markdown_report(reports, batch_at_risk_paise, drift_reports, notes), encoding="utf-8"
    )
    return path
