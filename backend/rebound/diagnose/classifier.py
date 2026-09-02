"""The hybrid classifier: rules first, model only where rules abstain.

This ordering is the project's central claim about AI judgment. A language model
is the most expensive, slowest and least predictable component available, so it is
used on the smallest slice of traffic that genuinely needs it, and every row
records which path decided it.

Escalation ladder:

  1. sanitize the untrusted description
  2. deterministic rules            -> ~2 microseconds, no cost, high precision
  3. constrained model on the tail  -> only when rules abstain
  4. low-confidence floor           -> demote to UNKNOWN rather than guess
  5. hostile input                  -> always demoted, never silently trusted
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import List, Optional

from ..models import Diagnosis, FailedPayment
from ..taxonomy import FailureClass
from . import rules
from .llm import TailClassifier
from .sanitize import is_hostile, sanitize_untrusted

# Below this, we would rather admit ignorance than act on a coin flip. UNKNOWN is
# never retried and never contacted, so the cost of demotion is a missed recovery
# and the cost of a wrong guess is a wrong action - asymmetric, so we demote.
LOW_CONFIDENCE_FLOOR = 0.35


@dataclass
class ClassifierStats:
    """Counters for the ablation and cost tables in the report."""

    total: int = 0
    by_rules: int = 0
    by_model: int = 0
    demoted_low_confidence: int = 0
    demoted_hostile: int = 0
    sanitizer_flags: List[str] = field(default_factory=list)
    llm_calls: int = 0
    llm_cache_hits: int = 0
    llm_cost_usd: float = 0.0

    @property
    def deterministic_share(self) -> float:
        return self.by_rules / self.total if self.total else 0.0


class HybridClassifier:
    def __init__(self, tail: Optional[TailClassifier] = None, use_model: bool = True):
        self.tail = tail or TailClassifier()
        self.use_model = use_model
        self.stats = ClassifierStats()
        self._lock = threading.Lock()

    @property
    def provider_name(self) -> str:
        return self.tail.provider.name

    def diagnose(self, payment: FailedPayment) -> Diagnosis:
        clean, flags = sanitize_untrusted(payment.error_description or "")
        with self._lock:
            self.stats.total += 1
            for flag in flags:
                if flag not in self.stats.sanitizer_flags:
                    self.stats.sanitizer_flags.append(flag)

        diagnosis = rules.classify(payment, clean)
        if diagnosis is not None:
            with self._lock:
                self.stats.by_rules += 1
            diagnosis.flags = flags
            return self._apply_safety_floor(diagnosis, flags)

        if not self.use_model:
            # Rules-only mode, used by the ablation study. Abstaining is the
            # honest outcome here, not a fallback guess.
            return self._apply_safety_floor(
                Diagnosis(
                    payment_id=payment.payment_id,
                    failure_class=FailureClass.UNKNOWN,
                    confidence=0.0,
                    source="rules_abstain",
                    rationale="No deterministic rule matched and the model is disabled.",
                    flags=flags,
                ),
                flags,
            )

        diagnosis = self.tail.diagnose(payment, clean, flags)
        diagnosis.flags = flags
        with self._lock:
            self.stats.by_model += 1
            self.stats.llm_calls = self.tail.call_count
            self.stats.llm_cache_hits = self.tail.cache_hits
            self.stats.llm_cost_usd = self.tail.total_cost_usd
        return self._apply_safety_floor(diagnosis, flags)

    def _apply_safety_floor(self, diagnosis: Diagnosis, flags: List[str]) -> Diagnosis:
        """Two demotions that run regardless of which path produced the verdict."""
        if is_hostile(flags):
            # Someone tried to steer the classifier. Even if the label looks
            # right, this row should be seen by a human before money moves.
            with self._lock:
                self.stats.demoted_hostile += 1
            diagnosis.rationale = (
                "Injection markers present in gateway text ({}); demoted for review. ".format(
                    ",".join(f for f in flags if f != "truncated")
                )
                + diagnosis.rationale
            )
            diagnosis.failure_class = FailureClass.UNKNOWN
            diagnosis.confidence = 0.0
            return diagnosis

        if diagnosis.failure_class is not FailureClass.UNKNOWN and (
            diagnosis.confidence < LOW_CONFIDENCE_FLOOR
        ):
            with self._lock:
                self.stats.demoted_low_confidence += 1
            diagnosis.rationale = (
                "Confidence {:.2f} below floor {:.2f}; demoted to unknown. ".format(
                    diagnosis.confidence, LOW_CONFIDENCE_FLOOR
                )
                + diagnosis.rationale
            )
            diagnosis.failure_class = FailureClass.UNKNOWN

        return diagnosis
