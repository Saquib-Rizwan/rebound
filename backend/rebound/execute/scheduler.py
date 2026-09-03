"""Fires the actions the agent promised for later.

Deciding "retry this in one hour" and never doing it is not a recovery agent, it
is a recommendation engine with extra steps. This is the component that closes
that gap, and it exists because the decision layer deliberately schedules most of
its work into the future - for bank downtime the delay *is* the intervention.

The design point worth arguing for:

**Guardrails are re-evaluated at fire time, not trusted from decision time.**

A decision made twenty-four hours ago was correct given what was known then. By
the time it fires, the customer may have been messaged about something else, the
merchant's daily budget may be spent, the kill switch may be on, or the payment
may have already been recovered. Firing on a stale authorisation is how automated
systems end up messaging someone at 3am about a payment they completed yesterday.

So a scheduled action carries permission to be *considered* at its due time, never
permission to happen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from .. import config
from ..ingest.razorpay_client import GatewayClient, build_gateway
from ..ledger.store import Ledger
from ..taxonomy import Channel, FailureClass, InterventionType
from .messages import render_message


@dataclass
class FireResult:
    payment_id: str
    intervention: str
    fired: bool
    detail: str


class Scheduler:
    def __init__(
        self,
        ledger: Ledger,
        gateway: Optional[GatewayClient] = None,
        dry_run: Optional[bool] = None,
    ):
        self.ledger = ledger
        self.gateway = gateway or build_gateway()
        self.dry_run = config.DRY_RUN if dry_run is None else dry_run

    def due(self, now: datetime, limit: int = 200) -> List[Dict[str, Any]]:
        """Actions whose time has come and which have not been fired yet."""
        return self.ledger.query(
            "SELECT e.id, e.decision_id, e.payment_id, e.intervention, e.scheduled_for, "
            "       d.amount_paise, d.failure_class, d.channel, d.merchant_id, "
            "       d.customer_id, d.explanation "
            "FROM executions e JOIN decisions d ON d.decision_id = e.decision_id "
            "WHERE e.fired_at IS NULL AND e.executed = 1 "
            "  AND e.scheduled_for IS NOT NULL AND e.scheduled_for <= ? "
            "ORDER BY e.scheduled_for LIMIT ?",
            (now.isoformat(timespec="seconds"), limit),
        )

    def _still_valid(self, row: Dict[str, Any]) -> Optional[str]:
        """Re-check the world at fire time. Returns a reason to cancel, or None."""
        if config.DRY_RUN and not self.dry_run:
            return "dry_run_flipped_on"

        # Already recovered? Then chasing it is worse than useless - it is a
        # message to someone who has already paid.
        recovered = self.ledger.query(
            "SELECT 1 FROM outcomes WHERE payment_id = ? AND recovered = 1 LIMIT 1",
            (row["payment_id"],),
        )
        if recovered:
            return "already_recovered"

        # Attempt ceiling, counted across everything actually fired for this payment.
        fired = self.ledger.query(
            "SELECT COUNT(*) AS n FROM executions WHERE payment_id = ? AND fired_at IS NOT NULL",
            (row["payment_id"],),
        )[0]["n"]
        if fired >= config.MAX_ATTEMPTS_PER_PAYMENT:
            return "max_attempts_reached"

        # Quiet hours are a property of the moment of sending, so they have to be
        # judged now rather than when the decision was made.
        if row["channel"] and row["channel"] != Channel.NONE.value:
            hour = datetime.now().hour
            start, end = config.QUIET_HOURS_START, config.QUIET_HOURS_END
            in_quiet = hour >= start or hour < end if start > end else start <= hour < end
            if in_quiet:
                return "quiet_hours_at_fire_time"

        return None

    def fire_one(self, row: Dict[str, Any]) -> FireResult:
        cancel = self._still_valid(row)
        if cancel:
            self.ledger.conn.execute(
                "UPDATE executions SET fired_at = ?, fire_result = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), "cancelled: " + cancel, row["id"]),
            )
            self.ledger.conn.commit()
            return FireResult(row["payment_id"], row["intervention"], False, "cancelled: " + cancel)

        intervention = InterventionType(row["intervention"])
        if self.dry_run:
            detail = "dry_run_fired"
        elif intervention in (InterventionType.RETRY_NOW, InterventionType.RETRY_SCHEDULED):
            result = self.gateway.schedule_retry(
                payment_id=row["payment_id"],
                order_id="order_for_" + row["payment_id"],
                amount_paise=row["amount_paise"],
                when=datetime.now(),
            )
            detail = ("fired: " + str(result.reference)) if result.ok else ("failed: " + str(result.error))
        else:
            result = self.gateway.create_payment_link(
                payment_id=row["payment_id"],
                amount_paise=row["amount_paise"],
                description="Complete your payment",
                expire_in_hours=72.0,
            )
            detail = ("fired: " + str(result.short_url or result.reference)) if result.ok \
                else ("failed: " + str(result.error))

        self.ledger.conn.execute(
            "UPDATE executions SET fired_at = ?, fire_result = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), detail, row["id"]),
        )
        self.ledger.conn.commit()
        return FireResult(
            row["payment_id"], row["intervention"], detail.startswith(("fired", "dry_run")), detail
        )

    def tick(self, now: Optional[datetime] = None, limit: int = 200) -> List[FireResult]:
        """One pass over everything due. Safe to run repeatedly."""
        return [self.fire_one(row) for row in self.due(now or datetime.now(), limit)]

    def pending_summary(self) -> List[Dict[str, Any]]:
        return self.ledger.query(
            "SELECT intervention, COUNT(*) AS n, MIN(scheduled_for) AS next_due "
            "FROM executions WHERE fired_at IS NULL AND scheduled_for IS NOT NULL "
            "GROUP BY intervention ORDER BY n DESC"
        )
