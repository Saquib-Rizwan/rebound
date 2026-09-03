"""RBI Digital Payments E-Mandate Framework compliance.

Recurring debits in India are regulated, and the rules constrain exactly what a
recovery agent wants to do. These are not preferences the expected-value model may
trade away - a retry that breaches them is unlawful regardless of how much money it
would have made, which is why they live in the guardrail layer.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from rebound import config
from rebound.models import Diagnosis
from rebound.policy import engine
from rebound.policy.guardrails import AgentState
from rebound.sim.generator import generate_batch
from rebound.taxonomy import FailureClass, InterventionType, Rail

RETRIES = {InterventionType.RETRY_NOW, InterventionType.RETRY_SCHEDULED}
NOW = datetime(2026, 9, 4, 14, 0)


def _recurring(amount_paise: int):
    payments, _ = generate_batch(n=5, seed=21)
    return payments[0].model_copy(update={
        "is_recurring": True, "rail": Rail.EMANDATE, "amount": amount_paise,
        "attempt_number": 1,
        "contact_consent": {"whatsapp": True, "sms": True, "email": True},
    })


def _diagnose(payment, failure_class=FailureClass.INSUFFICIENT_FUNDS):
    return Diagnosis(payment_id=payment.payment_id, failure_class=failure_class,
                     confidence=0.95, source="test")


def test_recurring_retry_never_runs_before_the_pre_debit_notice():
    """RBI requires the notification 24 hours ahead, so no sooner retry is legal."""
    payment = _recurring(90_000)
    decision = engine.decide(payment, _diagnose(payment), AgentState(), NOW)

    for candidate in decision.considered:
        if candidate.intervention in RETRIES and candidate.blocked_by is None:
            assert candidate.delay_hours >= config.PRE_DEBIT_NOTICE_HOURS

    if decision.chosen.intervention in RETRIES:
        assert decision.chosen.delay_hours >= config.PRE_DEBIT_NOTICE_HOURS


def test_recurring_debit_above_afa_threshold_is_never_silently_retried():
    """Above the threshold the customer must re-authenticate, so no silent retry."""
    payment = _recurring(config.AFA_THRESHOLD_PAISE + 1)
    decision = engine.decide(payment, _diagnose(payment), AgentState(), NOW)

    assert decision.chosen.intervention not in RETRIES
    blocked = {c.blocked_by for c in decision.considered if c.blocked_by}
    assert "G12_afa_required" in blocked


def test_large_recurring_debit_still_has_a_lawful_recovery_path():
    """Compliance must not mean paralysis - an authenticated path stays open."""
    payment = _recurring(4_000_000)
    decision = engine.decide(payment, _diagnose(payment), AgentState(), NOW)
    assert decision.chosen.intervention in {
        InterventionType.NUDGE_LINK, InterventionType.SWITCH_RAIL,
        InterventionType.REQUEST_REMANDATE, InterventionType.SUPPRESS,
    }


def test_one_off_payments_are_not_subject_to_mandate_rules():
    """The rails must not leak onto non-recurring payments and block ordinary retries."""
    payments, _ = generate_batch(n=5, seed=21)
    payment = payments[0].model_copy(update={
        "is_recurring": False, "rail": Rail.CARD, "amount": 4_000_000,
        "attempt_number": 1,
    })
    decision = engine.decide(
        payment, _diagnose(payment, FailureClass.TECHNICAL_ERROR), AgentState(), NOW)
    blocked = {c.blocked_by for c in decision.considered if c.blocked_by}
    assert "G11_pre_debit_notice" not in blocked
    assert "G12_afa_required" not in blocked


@pytest.mark.parametrize("amount", [1_000, 500_000, config.AFA_THRESHOLD_PAISE])
def test_below_threshold_recurring_retries_remain_available(amount):
    payment = _recurring(amount)
    decision = engine.decide(payment, _diagnose(payment), AgentState(), NOW)
    allowed = [c for c in decision.considered
               if c.intervention in RETRIES and c.blocked_by is None]
    assert allowed, "no lawful retry window offered at INR {:,}".format(amount // 100)
