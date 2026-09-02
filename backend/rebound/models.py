"""Domain models. Everything crossing a component boundary is one of these."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .taxonomy import Channel, FailureClass, InterventionType, Rail


class FailedPayment(BaseModel):
    """A payment attempt that did not succeed, as it arrives from the gateway.

    Field names mirror the Razorpay Payments API where they overlap, so the real
    client and the mock client can hand back the same shape.
    """

    payment_id: str
    order_id: str
    customer_id: str
    merchant_id: str
    amount: int                      # paise, like the Razorpay API
    currency: str = "INR"
    rail: Rail
    method_detail: Optional[str] = None       # e.g. "HDFC credit", "okaxis"
    error_code: Optional[str] = None          # structured gateway code
    error_reason: Optional[str] = None        # structured sub-reason
    error_description: Optional[str] = None   # free text from issuer/PSP - the messy part
    created_at: datetime
    attempt_number: int = 1
    is_recurring: bool = False
    customer_ltv_paise: int = 0
    prior_success_count: int = 0
    contact_consent: Dict[str, bool] = Field(default_factory=dict)  # channel -> consented

    @property
    def amount_rupees(self) -> float:
        return self.amount / 100.0


class Diagnosis(BaseModel):
    """Root-cause verdict, plus the provenance needed to audit it."""

    payment_id: str
    failure_class: FailureClass
    confidence: float
    source: str                       # "rules" | "gemini" | "anthropic" | "offline_tfidf"
    rule_id: Optional[str] = None
    rationale: str = ""
    flags: List[str] = Field(default_factory=list)   # sanitizer findings, audit-visible
    llm_tokens: int = 0
    llm_cost_usd: float = 0.0
    latency_ms: float = 0.0


class ActionCandidate(BaseModel):
    """One option the policy engine scored, kept even if not chosen (audit trail)."""

    intervention: InterventionType
    delay_hours: float = 0.0
    channel: Channel = Channel.NONE
    target_rail: Optional[Rail] = None
    p_recover: float = 0.0
    gross_value_paise: float = 0.0
    cost_paise: float = 0.0
    annoyance_paise: float = 0.0
    expected_value_paise: float = 0.0
    blocked_by: Optional[str] = None   # guardrail id, if this option was ruled out


class Decision(BaseModel):
    """What the agent decided to do, and why. One row per payment per cycle."""

    payment_id: str
    merchant_id: str
    customer_id: str
    amount_paise: int
    diagnosis: Diagnosis
    chosen: ActionCandidate
    considered: List[ActionCandidate]
    guardrails_applied: List[str] = Field(default_factory=list)
    policy_version: str
    decided_at: datetime
    explanation: str = ""


class ExecutionResult(BaseModel):
    """What actually happened when the decision was executed."""

    payment_id: str
    intervention: InterventionType
    executed: bool
    idempotency_key: str
    external_ref: Optional[str] = None    # e.g. Razorpay payment-link id
    scheduled_for: Optional[datetime] = None
    error: Optional[str] = None
    dry_run: bool = True
    executed_at: datetime


class Outcome(BaseModel):
    """Ground truth from the simulator: did the money actually come back?"""

    payment_id: str
    recovered: bool
    recovered_amount_paise: int
    hours_to_recovery: Optional[float] = None
    customer_contacts: int = 0
    action_cost_paise: float = 0.0


class BatchMetrics(BaseModel):
    """Headline numbers for one policy over one batch. This is the scoreboard."""

    policy_name: str
    payments: int
    at_risk_paise: int
    recovered_count: int
    recovered_paise: int
    recovery_rate: float
    action_cost_paise: float
    net_recovered_paise: float
    customer_contacts: int
    contacts_per_recovery: float
    suppressed_count: int
    llm_cost_usd: float = 0.0
    llm_call_count: int = 0
    deterministic_share: float = 0.0
    ci_low_paise: Optional[float] = None
    ci_high_paise: Optional[float] = None
