"""Quality gates. The build fails if the agent gets measurably worse.

Thresholds are set below current measured performance, not at it, so ordinary
variation does not turn the build red - but a real regression does. Each one says
what it is protecting and what the number was when it was set.

These run against the offline classifier so they are deterministic, free, and need
no network. That makes the gate honest: it measures the floor the system holds
without any model at all.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from rebound.diagnose.classifier import HybridClassifier
from rebound.diagnose.llm import OfflineProvider, TailClassifier
from rebound.diagnose.error_cost import error_cost_paise, worst_confusions
from rebound.diagnose.evaluate import run_variant
from rebound.policy import engine
from rebound.policy.guardrails import AgentState
from rebound.sim.baselines import POLICY_ORDER
from rebound.sim.evaluate_policy import compare
from rebound.sim.generator import generate_batch
from rebound.taxonomy import FailureClass, InterventionType

# Measured 2026-09-03 with the offline classifier: accuracy 0.925, macro F1 0.951.
MIN_RULES_ACCURACY = 0.85
MIN_RULES_MACRO_F1 = 0.88


@pytest.fixture(scope="module")
def batch():
    return generate_batch(n=250, seed=7)


def test_rules_only_holds_its_floor(batch):
    payments, truth = batch
    report = run_variant("rules_only", payments, truth, use_model=False)
    assert report.accuracy >= MIN_RULES_ACCURACY, (
        "rules accuracy fell to {:.3f}".format(report.accuracy))
    assert report.macro_f1 >= MIN_RULES_MACRO_F1


def test_rules_are_precise_when_they_answer(batch):
    """The rules may abstain freely, but must not be confidently wrong.

    This is the property the whole escalation design rests on: an abstention costs
    one model call, a confident error costs a wrong action on a real payment.
    """
    payments, truth = batch
    report = run_variant("rules_only", payments, truth, use_model=False)
    assert report.accuracy_when_answered >= 0.95


def test_fraud_misclassification_is_priced_as_catastrophic():
    """The error-cost model must keep its ordering, whatever the constants become."""
    cheap = error_cost_paise(
        FailureClass.INSUFFICIENT_FUNDS, FailureClass.SUSPECTED_FRAUD, 100_000)
    catastrophic = error_cost_paise(
        FailureClass.SUSPECTED_FRAUD, FailureClass.INSUFFICIENT_FUNDS, 100_000)
    assert catastrophic > cheap * 10

    worst = worst_confusions(100_000, top_n=3)
    assert all(true == FailureClass.SUSPECTED_FRAUD.value for true, _, _ in worst)


def test_agent_beats_doing_nothing(batch):
    """The headline claim, as a gate. Measured uplift was +INR 68,617."""
    payments, truth = batch
    payments = sorted(payments, key=lambda p: p.created_at)
    classifier = HybridClassifier(tail=TailClassifier(OfflineProvider()))
    diagnoses = {p.payment_id: classifier.diagnose(p) for p in payments}

    summaries = compare(payments, diagnoses, truth, replications=8)
    agent, nothing = summaries["rebound"], summaries["do_nothing"]

    assert agent.mean_net() > nothing.mean_net()
    assert agent.beats(nothing) >= 0.9, (
        "agent only beat do-nothing in {:.0%} of replications".format(agent.beats(nothing)))


def test_agent_is_quieter_than_messaging_everyone(batch):
    """Our actual claim against the strongest baseline is restraint, not revenue."""
    payments, truth = batch
    payments = sorted(payments, key=lambda p: p.created_at)
    classifier = HybridClassifier(tail=TailClassifier(OfflineProvider()))
    diagnoses = {p.payment_id: classifier.diagnose(p) for p in payments}

    summaries = compare(payments, diagnoses, truth, replications=8)
    agent, nudge_all = summaries["rebound"], summaries["nudge_all"]

    assert agent.mean("contacts") < nudge_all.mean("contacts") * 0.75
    assert agent.mean("churned") <= nudge_all.mean("churned")


def test_agent_stays_silent_on_a_meaningful_share(batch):
    """If suppression ever drops to nothing, the economics have broken."""
    payments, _ = batch
    payments = sorted(payments, key=lambda p: p.created_at)
    classifier = HybridClassifier(tail=TailClassifier(OfflineProvider()))
    state = AgentState()
    suppressed = 0
    for payment in payments:
        diagnosis = classifier.diagnose(payment)
        decision = engine.decide(
            payment, diagnosis, state, payment.created_at + timedelta(minutes=15))
        engine.apply_to_state(decision, state)
        suppressed += decision.chosen.intervention is InterventionType.SUPPRESS
    assert 0.05 <= suppressed / len(payments) <= 0.60


def test_all_policies_are_comparable(batch):
    payments, truth = batch
    classifier = HybridClassifier(tail=TailClassifier(OfflineProvider()))
    diagnoses = {p.payment_id: classifier.diagnose(p) for p in payments}
    summaries = compare(payments, diagnoses, truth, replications=4)
    assert set(summaries) == set(POLICY_ORDER)
    for name, summary in summaries.items():
        assert summary.n_payments == len(payments), name
