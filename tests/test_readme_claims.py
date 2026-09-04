"""The README is tested.

Every headline number in README.md is also produced by code in this repo, and the two
drift apart the moment anything changes. That is not hypothetical: during development
the README claimed 88 suppressed payments and 1,702 blocked actions while the code had
moved to 90 and 1,806, and it described the scheduler and the calibration loop as
future work after both had been built.

Stale numbers in a README are worse than missing ones. A reviewer who checks one
figure and finds it wrong stops believing the rest, and they are right to.

So the claims are asserted here. If a number in the README no longer matches what the
code produces, the build fails and one of the two has to change.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rebound.policy.guardrails import ALL_GUARDRAILS
from rebound.taxonomy import FailureClass, InterventionType

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
RECOVERY_JSON = ROOT / "reports" / "recovery.json"


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def recovery():
    if not RECOVERY_JSON.exists():
        pytest.skip("run `python rebound.py eval-policy` to generate reports/recovery.json")
    return json.loads(RECOVERY_JSON.read_text(encoding="utf-8"))


def _inr(paise: float) -> str:
    return "{:,.0f}".format(paise / 100)


def _policy(recovery, name):
    return next(p for p in recovery["policies"] if p["name"] == name)


# ---------------------------------------------------------------- structural counts
def test_guardrail_count_matches(readme):
    n = len(ALL_GUARDRAILS)
    assert "{} hard rules".format(_word(n)) in readme.lower() or str(n) in readme, (
        "README does not state the current guardrail count of {}".format(n))


def _word(n: int) -> str:
    return {11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen"}.get(n, str(n))


def test_taxonomy_counts_match(readme):
    assert "{} failure classes".format(len(list(FailureClass))) in readme
    assert "{} actions".format(len(list(InterventionType))) in readme


def test_postmortem_count_matches(readme):
    entries = len(re.findall(r"^## \d+\.", (ROOT / "POSTMORTEM.md").read_text(encoding="utf-8"),
                             flags=re.M))
    words = {6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten"}
    assert "{} things that broke".format(words.get(entries, entries)) in readme, (
        "POSTMORTEM has {} entries; README says otherwise".format(entries))


# ------------------------------------------------------------------ headline money
def test_uplift_against_doing_nothing_matches(readme, recovery):
    claimed = _inr(recovery["headline"]["uplift_vs_nothing_paise"])
    assert claimed in readme, (
        "README should state an uplift of INR {} against do_nothing".format(claimed))


def test_confidence_interval_matches(readme, recovery):
    for key in ("uplift_ci_low_paise", "uplift_ci_high_paise"):
        claimed = _inr(recovery["headline"][key])
        assert claimed in readme, "README is missing CI bound INR {}".format(claimed)


def test_uplift_against_best_naive_matches(readme, recovery):
    claimed = _inr(recovery["headline"]["uplift_vs_best_naive_paise"])
    assert claimed in readme


def test_contact_counts_match(readme, recovery):
    agent = "{:.0f}".format(_policy(recovery, "rebound")["contacts"])
    blanket = "{:.0f}".format(_policy(recovery, "nudge_all")["contacts"])
    assert agent in readme, "README should state {} contacts for the agent".format(agent)
    assert blanket in readme, "README should state {} contacts for nudge_all".format(blanket)


def test_suppression_count_matches(readme, recovery):
    claimed = "{:.0f}".format(_policy(recovery, "rebound")["suppressed"])
    assert "{} of 400".format(claimed) in readme, (
        "README should say the agent suppressed {} of 400".format(claimed))


# ------------------------------------------------------------------- honesty guards
def test_readme_discloses_that_outcomes_are_simulated(readme):
    """The most important sentence in the document. It must not be edited away."""
    lowered = readme.lower()
    assert "simulated outcomes" in lowered
    assert "no real payment was recovered" in lowered
    # and it must appear early, not buried at the bottom
    assert lowered.index("no real payment was recovered") < len(readme) * 0.25


def test_readme_states_where_the_approach_fails(readme):
    """Claims without limits read as marketing. The boundary has to stay stated."""
    lowered = readme.lower()
    assert "crosses zero" in lowered, "README must keep the honest nudge_all comparison"
    assert "shadow mode" in lowered, "README must keep what would replace the simulator"


def test_future_work_does_not_claim_built_features(readme):
    """Guards the exact mistake made during development.

    The README described the scheduler and the calibration loop as future work after
    both had shipped. Anything listed under 'What I would build next' must not be a
    module that already exists.
    """
    tail = readme.split("## What I would build next")[-1].lower()
    built = {
        "scheduler.py": "execute/scheduler.py",
        "calibration.py": "policy/calibration.py",
        "insights.py": "analytics/insights.py",
    }
    for module, path in built.items():
        assert (ROOT / "backend" / "rebound" / path).exists(), path
    # Naming a built module as future work is only allowed alongside a qualifier
    # explaining what specifically is still missing.
    if "scheduler" in tail:
        assert "runtime" in tail or "durable queue" in tail, (
            "future work mentions the scheduler, which exists - say what is still missing")
    if "calibration" in tail:
        assert "merchant" in tail or "scope" in tail, (
            "future work mentions calibration, which exists - say what is still missing")
