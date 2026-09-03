"""Ledger behaviour: idempotency and the observed/simulated separation."""
from __future__ import annotations

from datetime import datetime

from rebound.execute.executor import Executor
from rebound.ingest.razorpay_client import MockGateway
from rebound.ledger.store import Ledger, idempotency_key
from rebound.models import ActionCandidate, Decision, Diagnosis
from rebound.taxonomy import Channel, FailureClass, InterventionType


def _decision(payment_id="pay_T1", intervention=InterventionType.NUDGE_LINK):
    return Decision(
        payment_id=payment_id, merchant_id="mrch_x", customer_id="cust_x",
        amount_paise=100000,
        diagnosis=Diagnosis(payment_id=payment_id, failure_class=FailureClass.AUTH_DROPOFF,
                            confidence=0.9, source="test"),
        chosen=ActionCandidate(intervention=intervention, channel=Channel.WHATSAPP,
                               expected_value_paise=500),
        considered=[], policy_version="test-v1", decided_at=datetime(2026, 9, 2, 12, 0),
    )


def test_idempotency_key_ignores_time(tmp_path):
    """Two runs of the same batch must not message a customer twice."""
    a = _decision()
    b = a.model_copy(update={"decided_at": datetime(2026, 9, 3, 18, 0)})
    assert idempotency_key(a) == idempotency_key(b)


def test_replaying_a_decision_executes_once(tmp_path):
    ledger = Ledger(tmp_path / "t.sqlite3")
    ledger.start_run("r1", "test-v1", "offline", "m", True, 1)
    executor = Executor(ledger, gateway=MockGateway(failure_rate=0.0), dry_run=True)

    decision = _decision()
    did = ledger.record_decision("r1", decision)

    first = executor.execute(decision, did)
    second = executor.execute(decision, did)

    assert first.executed is True
    assert second.executed is False
    assert "already executed" in (second.error or "")
    assert len(ledger.query("SELECT 1 FROM executions")) == 1


def test_suppression_is_recorded_not_skipped(tmp_path):
    ledger = Ledger(tmp_path / "t.sqlite3")
    ledger.start_run("r1", "test-v1", "offline", "m", True, 1)
    executor = Executor(ledger, gateway=MockGateway(), dry_run=True)

    decision = _decision(intervention=InterventionType.SUPPRESS)
    did = ledger.record_decision("r1", decision)
    executor.execute(decision, did)

    rows = ledger.query("SELECT intervention, executed FROM executions")
    assert len(rows) == 1
    assert rows[0]["intervention"] == "suppress"
    assert rows[0]["executed"] == 0


def test_observed_and_simulated_outcomes_stay_separate(tmp_path):
    from rebound.models import Outcome

    ledger = Ledger(tmp_path / "t.sqlite3")
    ledger.start_run("r1", "test-v1", "offline", "m", True, 2)
    ledger.record_outcomes("r1", [Outcome(payment_id="pay_sim", recovered=True,
                                          recovered_amount_paise=1000)], "simulated")
    ledger.record_observed_recovery("pay_real", 2000)

    split = {row["source"]: row for row in ledger.outcome_split()}
    assert set(split) == {"simulated", "observed"}
    assert split["observed"]["paise"] == 2000
    assert split["simulated"]["paise"] == 1000


def test_candidates_persist_including_blocked_ones(tmp_path):
    ledger = Ledger(tmp_path / "t.sqlite3")
    ledger.start_run("r1", "test-v1", "offline", "m", True, 1)
    decision = _decision()
    decision.considered = [
        ActionCandidate(intervention=InterventionType.SUPPRESS),
        ActionCandidate(intervention=InterventionType.NUDGE_LINK, channel=Channel.WHATSAPP,
                        expected_value_paise=500),
        ActionCandidate(intervention=InterventionType.RETRY_NOW, blocked_by="G01_never_retry_class"),
    ]
    ledger.record_decision("r1", decision)
    rows = ledger.query("SELECT intervention, blocked_by FROM candidates")
    assert len(rows) == 3
    assert any(r["blocked_by"] == "G01_never_retry_class" for r in rows)
