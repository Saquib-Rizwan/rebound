"""Turns a Decision into an action, or refuses to.

Three properties this layer must have, in priority order:

1. **Safe by default.** ``config.DRY_RUN`` is on unless someone explicitly turns
   it off. Nothing reaches a gateway by accident.
2. **Idempotent.** The action key excludes time, and the ledger enforces
   uniqueness, so replaying a batch cannot message a customer twice. Cron jobs
   double-fire; agents get restarted mid-run. This is not hypothetical.
3. **Loud on failure.** A gateway error is recorded as a failed execution against
   the decision, never swallowed. A recovery agent that quietly fails to recover
   is worse than no agent, because the merchant thinks it is working.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .. import config
from ..ingest.razorpay_client import GatewayClient, build_gateway
from ..ledger.store import Ledger, idempotency_key
from ..models import Decision, ExecutionResult
from ..taxonomy import InterventionType
from .messages import render_message

# Actions that reach the customer through a payment link.
LINK_ACTIONS = frozenset({
    InterventionType.NUDGE_LINK,
    InterventionType.SWITCH_RAIL,
    InterventionType.REQUEST_REMANDATE,
})
RETRY_ACTIONS = frozenset({
    InterventionType.RETRY_NOW,
    InterventionType.RETRY_SCHEDULED,
})


class Executor:
    def __init__(
        self,
        ledger: Ledger,
        gateway: Optional[GatewayClient] = None,
        dry_run: Optional[bool] = None,
    ):
        self.ledger = ledger
        self.gateway = gateway or build_gateway()
        self.dry_run = config.DRY_RUN if dry_run is None else dry_run
        self.executed = 0
        self.skipped_idempotent = 0
        self.failed = 0

    def execute(self, decision: Decision, decision_id_value: str) -> ExecutionResult:
        chosen = decision.chosen
        key = idempotency_key(decision)
        now = datetime.now()

        # Suppression is a real outcome and gets a row. "We deliberately did
        # nothing" must be as auditable as "we sent a link".
        if chosen.intervention is InterventionType.SUPPRESS:
            return self._record(decision_id_value, ExecutionResult(
                payment_id=decision.payment_id,
                intervention=chosen.intervention,
                executed=False,
                idempotency_key=key,
                dry_run=self.dry_run,
                executed_at=now,
                error=None,
            ))

        existing = self.ledger.already_executed(key)
        if existing is not None:
            self.skipped_idempotent += 1
            return ExecutionResult(
                payment_id=decision.payment_id,
                intervention=chosen.intervention,
                executed=False,
                idempotency_key=key,
                external_ref=existing,
                dry_run=self.dry_run,
                executed_at=now,
                error="skipped: already executed",
            )

        scheduled_for = now + timedelta(hours=chosen.delay_hours)

        if chosen.intervention is InterventionType.ESCALATE_HUMAN:
            # No gateway call - this is a queue for a person. Recorded as executed
            # because the handoff genuinely happened.
            self.executed += 1
            return self._record(decision_id_value, ExecutionResult(
                payment_id=decision.payment_id,
                intervention=chosen.intervention,
                executed=True,
                idempotency_key=key,
                external_ref="queue:human_review",
                scheduled_for=now,
                dry_run=self.dry_run,
                executed_at=now,
            ))

        if self.dry_run:
            self.executed += 1
            return self._record(decision_id_value, ExecutionResult(
                payment_id=decision.payment_id,
                intervention=chosen.intervention,
                executed=True,
                idempotency_key=key,
                external_ref="dryrun:" + key[-10:],
                scheduled_for=scheduled_for,
                dry_run=True,
                executed_at=now,
            ))

        if chosen.intervention in LINK_ACTIONS:
            message = render_message(decision)
            result = self.gateway.create_payment_link(
                payment_id=decision.payment_id,
                amount_paise=decision.amount_paise,
                description=message.subject,
                expire_in_hours=max(24.0, chosen.delay_hours + 48.0),
            )
        elif chosen.intervention in RETRY_ACTIONS:
            result = self.gateway.schedule_retry(
                payment_id=decision.payment_id,
                order_id="order_for_" + decision.payment_id,
                amount_paise=decision.amount_paise,
                when=scheduled_for,
            )
        else:
            result = None

        if result is None or not result.ok:
            self.failed += 1
            return self._record(decision_id_value, ExecutionResult(
                payment_id=decision.payment_id,
                intervention=chosen.intervention,
                executed=False,
                idempotency_key=key,
                scheduled_for=scheduled_for,
                dry_run=False,
                executed_at=now,
                error=(result.error if result else "unsupported intervention"),
            ))

        self.executed += 1
        return self._record(decision_id_value, ExecutionResult(
            payment_id=decision.payment_id,
            intervention=chosen.intervention,
            executed=True,
            idempotency_key=key,
            external_ref=result.short_url or result.reference,
            scheduled_for=scheduled_for,
            dry_run=False,
            executed_at=now,
        ))

    def _record(self, decision_id_value: str, result: ExecutionResult) -> ExecutionResult:
        self.ledger.record_execution(decision_id_value, result)
        return result
