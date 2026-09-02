"""SQLite audit ledger. Append-only, queryable, and the source of truth for "why".

Deliberately SQLite and deliberately plain SQL: the point of an audit trail is that
someone other than its author can interrogate it. A merchant ops lead with a
sqlite3 prompt can answer "what did you do to my customers yesterday and why"
without running any of our code.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..models import Decision, ExecutionResult, Outcome

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def decision_id(decision: Decision) -> str:
    """Stable id: the same decision written twice collides instead of duplicating."""
    raw = "|".join([
        decision.payment_id,
        decision.policy_version,
        decision.decided_at.isoformat(),
        decision.chosen.intervention.value,
    ])
    return "dec_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def idempotency_key(decision: Decision) -> str:
    """Identifies the *action*, not the decision record.

    Deliberately excludes the timestamp: two runs of the same batch produce the
    same key, so replaying a batch cannot send a customer a second message. That
    is the property you want when a cron job double-fires at 3am.
    """
    chosen = decision.chosen
    raw = "|".join([
        decision.payment_id,
        chosen.intervention.value,
        str(chosen.delay_hours),
        chosen.channel.value,
        decision.policy_version,
    ])
    return "idem_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL keeps the dashboard readable while a batch is still writing.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------- writes
    def start_run(
        self,
        run_id: str,
        policy_version: str,
        provider: str,
        model: str,
        dry_run: bool,
        batch_size: int,
        notes: str = "",
    ) -> str:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, policy_version, provider, "
            "model, dry_run, batch_size, notes) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, datetime.now().isoformat(timespec="seconds"), policy_version,
             provider, model, int(dry_run), batch_size, notes),
        )
        self.conn.commit()
        return run_id

    def record_decision(self, run_id: str, decision: Decision) -> str:
        did = decision_id(decision)
        chosen = decision.chosen
        diag = decision.diagnosis

        self.conn.execute(
            "INSERT OR REPLACE INTO decisions (decision_id, run_id, payment_id, merchant_id, "
            "customer_id, amount_paise, failure_class, confidence, diag_source, rule_id, "
            "rationale, flags, llm_cost_usd, intervention, delay_hours, channel, target_rail, "
            "p_recover, gross_paise, cost_paise, annoyance_paise, ev_paise, guardrails, "
            "policy_version, decided_at, explanation) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                did, run_id, decision.payment_id, decision.merchant_id, decision.customer_id,
                decision.amount_paise, diag.failure_class.value, diag.confidence, diag.source,
                diag.rule_id, diag.rationale, json.dumps(diag.flags), diag.llm_cost_usd,
                chosen.intervention.value, chosen.delay_hours, chosen.channel.value,
                chosen.target_rail.value if chosen.target_rail else None,
                chosen.p_recover, chosen.gross_value_paise, chosen.cost_paise,
                chosen.annoyance_paise, chosen.expected_value_paise,
                json.dumps(decision.guardrails_applied), decision.policy_version,
                decision.decided_at.isoformat(timespec="seconds"), decision.explanation,
            ),
        )

        self.conn.execute("DELETE FROM candidates WHERE decision_id = ?", (did,))
        for candidate in decision.considered:
            is_chosen = (
                candidate.intervention is chosen.intervention
                and candidate.delay_hours == chosen.delay_hours
                and candidate.channel is chosen.channel
            )
            self.conn.execute(
                "INSERT INTO candidates (decision_id, intervention, delay_hours, channel, "
                "p_recover, cost_paise, annoyance_paise, ev_paise, blocked_by, chosen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, candidate.intervention.value, candidate.delay_hours,
                 candidate.channel.value, candidate.p_recover, candidate.cost_paise,
                 candidate.annoyance_paise, candidate.expected_value_paise,
                 candidate.blocked_by, int(is_chosen)),
            )
        self.conn.commit()
        return did

    def record_execution(self, decision_id_value: str, result: ExecutionResult) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO executions (decision_id, payment_id, intervention, executed, "
            "idempotency_key, external_ref, scheduled_for, error, dry_run, executed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id_value, result.payment_id, result.intervention.value,
                int(result.executed), result.idempotency_key, result.external_ref,
                result.scheduled_for.isoformat(timespec="seconds") if result.scheduled_for else None,
                result.error, int(result.dry_run),
                result.executed_at.isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def record_outcomes(self, run_id: str, outcomes: Iterable[Outcome], source: str) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO outcomes (payment_id, run_id, recovered, recovered_paise, "
            "hours_to_recovery, customer_contacts, action_cost_paise, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (o.payment_id, run_id, int(o.recovered), o.recovered_amount_paise,
                 o.hours_to_recovery, o.customer_contacts, o.action_cost_paise, source)
                for o in outcomes
            ],
        )
        self.conn.commit()

    def seen_event(self, event_id: str) -> bool:
        """At-least-once delivery means we must remember, not just check memory."""
        row = self.conn.execute(
            "SELECT 1 FROM webhook_events WHERE event_id = ? AND handled = 1", (event_id,)
        ).fetchone()
        return row is not None

    def record_webhook(
        self,
        event_id: str,
        event_type: str,
        signature_ok: bool,
        raw: str,
        payment_id: Optional[str] = None,
        handled: bool = False,
        error: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO webhook_events (event_id, event_type, received_at, "
            "signature_ok, payment_id, handled, error, raw) VALUES (?,?,?,?,?,?,?,?)",
            (event_id, event_type, datetime.now().isoformat(timespec="seconds"),
             int(signature_ok), payment_id, int(handled), error, raw[:20000]),
        )
        self.conn.commit()

    def record_observed_recovery(
        self, payment_id: str, amount_paise: int, hours_to_recovery: Optional[float] = None
    ) -> None:
        """An outcome we watched happen, not one we modelled.

        Marked `observed` so no report can ever blend measured recoveries with
        simulated ones without the distinction being visible in the data.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO outcomes (payment_id, run_id, recovered, "
            "recovered_paise, hours_to_recovery, customer_contacts, action_cost_paise, "
            "source) VALUES (?, NULL, 1, ?, ?, "
            "COALESCE((SELECT customer_contacts FROM outcomes WHERE payment_id = ?), 0), "
            "COALESCE((SELECT action_cost_paise FROM outcomes WHERE payment_id = ?), 0), "
            "'observed')",
            (payment_id, amount_paise, hours_to_recovery, payment_id, payment_id),
        )
        self.conn.commit()

    def outcome_split(self) -> List[Dict[str, Any]]:
        """Observed versus simulated. The ratio is the project's honesty metric."""
        return self.query(
            "SELECT source, COUNT(*) AS n, SUM(recovered) AS recovered, "
            "SUM(recovered_paise) AS paise FROM outcomes GROUP BY source"
        )

    def already_executed(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT external_ref FROM executions WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return row["external_ref"] if row else None

    # -------------------------------------------------------------------- reads
    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def action_mix(self, run_id: str) -> List[Dict[str, Any]]:
        return self.query(
            "SELECT intervention, COUNT(*) AS n, SUM(amount_paise) AS paise, "
            "ROUND(AVG(p_recover), 3) AS avg_p FROM decisions WHERE run_id = ? "
            "GROUP BY intervention ORDER BY n DESC",
            (run_id,),
        )

    def guardrail_hits(self, run_id: str) -> List[Dict[str, Any]]:
        """How often each rail actually stopped something. Zeros are a smell."""
        return self.query(
            "SELECT c.blocked_by AS guardrail, COUNT(*) AS blocked_candidates, "
            "COUNT(DISTINCT c.decision_id) AS payments FROM candidates c "
            "JOIN decisions d ON d.decision_id = c.decision_id "
            "WHERE d.run_id = ? AND c.blocked_by IS NOT NULL "
            "GROUP BY c.blocked_by ORDER BY blocked_candidates DESC",
            (run_id,),
        )

    def cost_of_caution(self, run_id: str) -> Dict[str, Any]:
        """Expected value the guardrails deliberately walked away from.

        Every safety rail has a price and most teams never measure theirs. This is
        the sum, over payments the agent suppressed, of the best expected value it
        was not permitted to pursue. It is not waste - it is what we paid for not
        messaging people at 3am - but it should be a number we can defend, not an
        unknown.
        """
        rows = self.query(
            "SELECT SUM(best) AS forgone, COUNT(*) AS payments FROM ("
            "  SELECT c.decision_id, MAX(c.ev_paise) AS best"
            "  FROM candidates c JOIN decisions d ON d.decision_id = c.decision_id"
            "  WHERE d.run_id = ? AND d.intervention = 'suppress'"
            "        AND c.blocked_by IS NOT NULL AND c.ev_paise > 0"
            "  GROUP BY c.decision_id)",
            (run_id,),
        )
        row = rows[0] if rows else {"forgone": 0, "payments": 0}
        by_rail = self.query(
            "SELECT c.blocked_by AS guardrail, ROUND(SUM(c.ev_paise)) AS ev_blocked "
            "FROM candidates c JOIN decisions d ON d.decision_id = c.decision_id "
            "WHERE d.run_id = ? AND d.intervention = 'suppress' AND c.blocked_by IS NOT NULL "
            "AND c.ev_paise > 0 GROUP BY c.blocked_by ORDER BY ev_blocked DESC",
            (run_id,),
        )
        return {
            "forgone_paise": row.get("forgone") or 0,
            "payments": row.get("payments") or 0,
            "by_guardrail": by_rail,
        }

    def explain_payment(self, payment_id: str) -> Dict[str, Any]:
        """Everything known about one payment. Powers the 'why not?' question."""
        decisions = self.query(
            "SELECT * FROM decisions WHERE payment_id = ? ORDER BY decided_at DESC",
            (payment_id,),
        )
        if not decisions:
            return {}
        latest = decisions[0]
        return {
            "decision": latest,
            "considered": self.query(
                "SELECT intervention, delay_hours, channel, p_recover, ev_paise, blocked_by, "
                "chosen FROM candidates WHERE decision_id = ? ORDER BY chosen DESC, ev_paise DESC",
                (latest["decision_id"],),
            ),
            "executions": self.query(
                "SELECT * FROM executions WHERE payment_id = ?", (payment_id,)
            ),
            "outcome": self.query(
                "SELECT * FROM outcomes WHERE payment_id = ?", (payment_id,)
            ),
        }
