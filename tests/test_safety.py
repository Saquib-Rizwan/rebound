"""Safety invariants. These are the tests that must never go red.

Everything here is a property that has to hold for *every* payment in the batch,
not a spot check on an example. If one of these fails, the agent is capable of
doing something to a real customer that it must never do, and the correct
response is to stop shipping rather than to adjust the threshold.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rebound import config
from rebound.diagnose.classifier import HybridClassifier
from rebound.diagnose.llm import OfflineProvider, TailClassifier
from rebound.models import Diagnosis
from rebound.policy import engine
from rebound.policy.guardrails import AgentState
from rebound.sim.generator import generate_batch
from rebound.taxonomy import NEVER_CONTACT, NEVER_RETRY, Channel, FailureClass, InterventionType

RETRIES = {InterventionType.RETRY_NOW, InterventionType.RETRY_SCHEDULED}
CONTACTS = {InterventionType.NUDGE_LINK, InterventionType.SWITCH_RAIL,
            InterventionType.REQUEST_REMANDATE}


@pytest.fixture(scope="module")
def decided():
    """One full agent pass, offline so the suite needs no credentials or network."""
    payments, truth = generate_batch(n=250, seed=7)
    payments.sort(key=lambda p: p.created_at)
    classifier = HybridClassifier(tail=TailClassifier(OfflineProvider()))
    state = AgentState()
    out = []
    for payment in payments:
        now = payment.created_at + timedelta(minutes=15)
        diagnosis = classifier.diagnose(payment)
        decision = engine.decide(payment, diagnosis, state, now)
        engine.apply_to_state(decision, state)
        out.append((payment, diagnosis, decision))
    return out


def test_never_retries_a_forbidden_class(decided):
    """The single most important invariant in the system."""
    for _, diagnosis, decision in decided:
        if diagnosis.failure_class in NEVER_RETRY:
            assert decision.chosen.intervention not in RETRIES, (
                "retried {} on {}".format(diagnosis.failure_class, decision.payment_id)
            )


def test_never_contacts_a_forbidden_class(decided):
    for _, diagnosis, decision in decided:
        if diagnosis.failure_class in NEVER_CONTACT:
            assert decision.chosen.intervention not in CONTACTS


def test_fraud_never_produces_an_actionable_candidate(decided):
    """Defence in depth: illegal options are not even proposed, let alone chosen."""
    for _, diagnosis, decision in decided:
        if diagnosis.failure_class is FailureClass.SUSPECTED_FRAUD:
            for candidate in decision.considered:
                assert candidate.intervention in (
                    InterventionType.SUPPRESS, InterventionType.ESCALATE_HUMAN
                )


def test_unknown_cause_is_never_acted_on(decided):
    for _, diagnosis, decision in decided:
        if diagnosis.failure_class is FailureClass.UNKNOWN:
            assert decision.chosen.intervention in (
                InterventionType.SUPPRESS, InterventionType.ESCALATE_HUMAN
            )


def test_no_contact_without_consent(decided):
    for payment, _, decision in decided:
        if decision.chosen.intervention in CONTACTS:
            channel = decision.chosen.channel
            assert channel is not Channel.NONE
            assert payment.contact_consent.get(channel.value) is True, (
                "messaged {} on unconsented {}".format(payment.customer_id, channel)
            )


def test_negative_expected_value_is_never_chosen(decided):
    """Suppress scores zero, so anything worse than nothing must lose to nothing."""
    for _, _, decision in decided:
        if decision.chosen.intervention is not InterventionType.ESCALATE_HUMAN:
            assert decision.chosen.expected_value_paise >= 0


def test_suppress_is_always_available_and_free(decided):
    for _, _, decision in decided:
        suppress = [c for c in decision.considered
                    if c.intervention is InterventionType.SUPPRESS]
        assert len(suppress) == 1
        assert suppress[0].expected_value_paise == 0
        assert suppress[0].cost_paise == 0


def test_attempt_ceiling_is_respected(decided):
    counts = {}
    for _, _, decision in decided:
        if decision.chosen.intervention is InterventionType.SUPPRESS:
            continue
        counts[decision.payment_id] = counts.get(decision.payment_id, 0) + 1
    assert all(n <= config.MAX_ATTEMPTS_PER_PAYMENT for n in counts.values())


def test_kill_switch_stops_everything():
    payments, _ = generate_batch(n=40, seed=3)
    state = AgentState()
    state.kill("test")
    for payment in payments:
        diagnosis = Diagnosis(
            payment_id=payment.payment_id, failure_class=FailureClass.AUTH_DROPOFF,
            confidence=0.95, source="test",
        )
        decision = engine.decide(payment, diagnosis, state, datetime(2026, 9, 2, 14, 0))
        assert decision.chosen.intervention is InterventionType.SUPPRESS


def test_quiet_hours_block_contact_but_not_retries():
    payments, _ = generate_batch(n=60, seed=5)
    night = datetime(2026, 9, 2, 23, 30)
    for payment in payments[:20]:
        diagnosis = Diagnosis(
            payment_id=payment.payment_id, failure_class=FailureClass.AUTH_DROPOFF,
            confidence=0.95, source="test",
        )
        decision = engine.decide(payment, diagnosis, AgentState(), night)
        chosen = decision.chosen
        if chosen.intervention in CONTACTS:
            # Permitted only if it has been deferred out of the quiet window.
            send_hour = (night + timedelta(hours=chosen.delay_hours)).hour
            assert not (send_hour >= config.QUIET_HOURS_START
                        or send_hour < config.QUIET_HOURS_END)
