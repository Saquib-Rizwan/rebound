"""Calibration and bounded exploration.

The sensitivity sweep says the policy degrades once its efficacy beliefs are
wrong. These tests cover the machinery that stops them being wrong: posteriors
that move with evidence, and exploration that is bounded rather than free.
"""
from __future__ import annotations

import random

from rebound.policy import economics
from rebound.policy.calibration import (EXPLORE_CEILING_PAISE, PRIOR_STRENGTH,
                                        Calibrator, Posterior)
from rebound.taxonomy import FailureClass, InterventionType

CLASS = FailureClass.AUTH_DROPOFF
ACTION = InterventionType.NUDGE_LINK


def test_starts_at_the_hand_written_prior():
    """On day one, with no evidence, the agent must behave exactly as before."""
    cal = Calibrator(explore=False)
    prior = economics.ACTION_EFFICACY[(CLASS, ACTION)]
    assert abs(cal.efficacy(CLASS, ACTION) - prior) < 0.01


def test_evidence_moves_the_belief_towards_the_truth():
    cal = Calibrator(explore=False)
    before = cal.efficacy(CLASS, ACTION)
    for _ in range(200):
        cal.update(CLASS, ACTION, recovered=True)
    after = cal.efficacy(CLASS, ACTION)
    assert after > before
    assert after > 0.85, "200 successes should dominate a weak prior"


def test_prior_is_weak_enough_to_be_overruled():
    """A prior that cannot be overruled is a constant wearing a costume."""
    assert PRIOR_STRENGTH <= 20
    cal = Calibrator(explore=False)
    for _ in range(60):
        cal.update(CLASS, ACTION, recovered=False)
    assert cal.efficacy(CLASS, ACTION) < 0.2


def test_exploration_is_bounded_by_ticket_value():
    """Learning is paid for with small payments, never with large ones."""
    cal = Calibrator(explore=True, seed=3)
    big = {cal.efficacy(CLASS, ACTION, EXPLORE_CEILING_PAISE * 10) for _ in range(40)}
    assert len(big) == 1, "large tickets must exploit deterministically"

    small = {cal.efficacy(CLASS, ACTION, 1_000) for _ in range(40)}
    assert len(small) > 1, "small tickets should sample, not exploit"


def test_exploration_narrows_as_confidence_grows():
    """Thompson sampling should stop exploring on its own, with no epsilon to tune."""
    def spread(observations: int) -> float:
        cal = Calibrator(explore=True, seed=11)
        for _ in range(observations):
            cal.update(CLASS, ACTION, recovered=True)
            cal.update(CLASS, ACTION, recovered=False)
        draws = [cal.efficacy(CLASS, ACTION, 1_000) for _ in range(300)]
        return max(draws) - min(draws)

    assert spread(400) < spread(0)


def test_never_invents_an_arm_that_does_not_exist():
    cal = Calibrator(explore=True)
    assert cal.efficacy(FailureClass.SUSPECTED_FRAUD, InterventionType.RETRY_NOW) == 0.0
    assert cal.efficacy(FailureClass.UNKNOWN, InterventionType.NUDGE_LINK) == 0.0


def test_posterior_reports_real_evidence_not_the_prior():
    post = Posterior(alpha=5.0, beta=7.0)
    assert post.observations == 0.0
    post.alpha += 10
    assert post.observations == 10.0


def test_drift_only_reports_arms_with_evidence():
    cal = Calibrator(explore=False)
    assert cal.drift() == {}
    for _ in range(30):
        cal.update(CLASS, ACTION, recovered=True)
    drift = cal.drift()
    assert len(drift) == 1
    entry = next(iter(drift.values()))
    assert entry["shift"] > 0
    assert entry["observations"] == 30
