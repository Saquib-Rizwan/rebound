"""HTTP surface: the webhook receiver and read-only views over the ledger.

The webhook handler is the only endpoint with real constraints on it, and they
come from how Razorpay behaves rather than from taste:

* **Acknowledge fast.** Razorpay expects a 2xx quickly and retries if it does not
  get one. Diagnosing a payment can involve a model call, which is far too slow to
  do inside the request. So the handler verifies, records, returns, and does the
  work in a background task.
* **Verify before parsing.** The signature covers the raw bytes. The body is read
  as bytes and checked before anything else looks at it.
* **A bad signature is a 400 that still gets written down.** Rejected, never
  processed, but always recorded - a spike in rejects is worth being able to see.
* **Dedup on the event id, in the database.** Retries and restarts both happen.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..diagnose.classifier import HybridClassifier
from ..execute.executor import Executor
from ..ingest import webhooks
from ..ledger.store import Ledger
from ..policy import engine
from ..policy.guardrails import AgentState

app = FastAPI(title="Rebound", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LIVE_RUN_ID = "run_live"

# Process-wide singletons. The agent's guardrail state must persist across
# requests or the cooldowns and budget caps would reset on every webhook, which
# would quietly disable most of the safety layer.
_ledger: Optional[Ledger] = None
_classifier: Optional[HybridClassifier] = None
_state: Optional[AgentState] = None
_executor: Optional[Executor] = None


def get_ledger() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger(config.DATA_DIR / "rebound.sqlite3")
    return _ledger


@app.on_event("startup")
def _startup() -> None:
    global _classifier, _state, _executor
    ledger = get_ledger()
    _classifier = HybridClassifier()
    _state = AgentState()
    _executor = Executor(ledger)
    ledger.start_run(
        run_id=LIVE_RUN_ID,
        policy_version=config.POLICY_VERSION,
        provider=_classifier.tail.provider.name,
        model=_classifier.tail.provider.model,
        dry_run=_executor.dry_run,
        batch_size=0,
        notes="live webhook-driven run",
    )


# ------------------------------------------------------------------- webhooks
def _handle_event(event: Dict[str, Any], event_id: str) -> None:
    """Runs after the response has been sent. Must never raise into the server."""
    ledger = get_ledger()
    event_type = event.get("event", "")
    payment_id = webhooks.event_payment_id(event)

    try:
        if event_type in webhooks.TRIGGER_EVENTS:
            payment = webhooks.parse_payment_failed(event)
            now = datetime.now()
            diagnosis = _classifier.diagnose(payment)
            decision = engine.decide(payment, diagnosis, _state, now)
            engine.apply_to_state(decision, _state)
            decision_id = ledger.record_decision(LIVE_RUN_ID, decision)
            _executor.execute(decision, decision_id)

        elif event_type in webhooks.OUTCOME_EVENTS:
            recovered_id, amount = webhooks.parse_outcome(event)
            # Only attribute a recovery to a payment we actually decided on.
            # A success we cannot tie to a decision is somebody else's payment,
            # and counting it would inflate our own results.
            known = recovered_id and ledger.query(
                "SELECT 1 FROM decisions WHERE payment_id = ? LIMIT 1", (recovered_id,)
            )
            if known:
                # The one place a real, observed outcome enters the system.
                ledger.record_observed_recovery(recovered_id, amount)
            else:
                ledger.record_webhook(
                    event_id, event_type, True, json.dumps(event), recovered_id,
                    handled=True, error="unattributed: no decision for this payment",
                )
                return

        ledger.record_webhook(
            event_id, event_type, True, json.dumps(event), payment_id, handled=True
        )
    except Exception as exc:  # noqa: BLE001 - a handler fault must not kill the receiver
        ledger.record_webhook(
            event_id, event_type, True, json.dumps(event), payment_id,
            handled=False, error="{}: {}".format(type(exc).__name__, exc),
        )


@app.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    background: BackgroundTasks,
    x_razorpay_signature: Optional[str] = Header(default=None),
    x_razorpay_event_id: Optional[str] = Header(default=None),
):
    raw = await request.body()

    if not config.RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="RAZORPAY_WEBHOOK_SECRET is not configured")

    if not webhooks.verify_signature(raw, x_razorpay_signature, config.RAZORPAY_WEBHOOK_SECRET):
        info = webhooks.summarise(raw)
        get_ledger().record_webhook(
            event_id=x_razorpay_event_id or "unsigned_{}".format(datetime.now().timestamp()),
            event_type=str(info.get("event")),
            signature_ok=False,
            raw=raw.decode("utf-8", errors="replace"),
            payment_id=info.get("payment_id"),
            handled=False,
            error="signature verification failed",
        )
        raise HTTPException(status_code=400, detail="invalid signature")

    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="body is not valid JSON")

    event_id = x_razorpay_event_id or "evt_{}".format(
        webhooks.compute_signature(raw, config.RAZORPAY_WEBHOOK_SECRET)[:24]
    )

    if get_ledger().seen_event(event_id):
        # Razorpay delivers at least once. A duplicate is expected, not an error.
        return JSONResponse({"status": "duplicate", "event_id": event_id})

    background.add_task(_handle_event, event, event_id)
    return JSONResponse({"status": "accepted", "event_id": event_id})


# ---------------------------------------------------------------- read models
@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "policy_version": config.POLICY_VERSION,
        "dry_run": _executor.dry_run if _executor else config.DRY_RUN,
        "provider": _classifier.tail.provider.name if _classifier else None,
        "webhook_secret_configured": bool(config.RAZORPAY_WEBHOOK_SECRET),
    }


@app.get("/runs")
def runs() -> Dict[str, Any]:
    return {"runs": get_ledger().query("SELECT * FROM runs ORDER BY started_at DESC")}


@app.get("/decisions")
def decisions(run_id: str = "run_smoke", limit: int = 100, intervention: str = "") -> Dict[str, Any]:
    sql = ("SELECT payment_id, amount_paise, failure_class, confidence, diag_source, "
           "intervention, delay_hours, channel, ev_paise, explanation, decided_at "
           "FROM decisions WHERE run_id = ?")
    params: tuple = (run_id,)
    if intervention:
        sql += " AND intervention = ?"
        params = (run_id, intervention)
    sql += " ORDER BY amount_paise DESC LIMIT ?"
    params = params + (limit,)
    return {"decisions": get_ledger().query(sql, params)}


@app.get("/decisions/{payment_id}")
def decision_detail(payment_id: str) -> Dict[str, Any]:
    detail = get_ledger().explain_payment(payment_id)
    if not detail:
        raise HTTPException(status_code=404, detail="no decision recorded for that payment")
    return detail


@app.get("/summary")
def summary(run_id: str = "run_smoke") -> Dict[str, Any]:
    ledger = get_ledger()
    return {
        "action_mix": ledger.action_mix(run_id),
        "guardrails": ledger.guardrail_hits(run_id),
        "cost_of_caution": ledger.cost_of_caution(run_id),
        "outcomes": ledger.outcome_split(),
    }


@app.get("/reports/recovery")
def recovery_report() -> Dict[str, Any]:
    """The policy-comparison numbers, machine readable.

    Written by `rebound.py eval-policy`. Served rather than recomputed because the
    comparison takes minutes and the dashboard should not be able to trigger it.
    """
    path = config.REPORTS_DIR / "recovery.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="no recovery report yet - run: python rebound.py eval-policy",
        )
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/insights")
def insights() -> Dict[str, Any]:
    """Systemic findings. Written by `rebound.py insights`."""
    path = config.REPORTS_DIR / "insights.json"
    if not path.exists():
        raise HTTPException(
            status_code=404, detail="run: python rebound.py insights"
        )
    return {"insights": json.loads(path.read_text(encoding="utf-8"))}


@app.get("/scheduled")
def scheduled() -> Dict[str, Any]:
    """The queue of promised-for-later actions, and what happened to them."""
    ledger = get_ledger()
    return {
        "pending": ledger.query(
            "SELECT intervention, COUNT(*) AS n, MIN(scheduled_for) AS next_due "
            "FROM executions WHERE fired_at IS NULL AND scheduled_for IS NOT NULL "
            "GROUP BY intervention ORDER BY n DESC"
        ),
        "fired": ledger.query(
            "SELECT fire_result, COUNT(*) AS n FROM executions "
            "WHERE fired_at IS NOT NULL GROUP BY fire_result ORDER BY n DESC LIMIT 12"
        ),
    }


@app.get("/webhooks/recent")
def recent_webhooks(limit: int = 25) -> Dict[str, Any]:
    return {
        "events": get_ledger().query(
            "SELECT event_id, event_type, received_at, signature_ok, payment_id, "
            "handled, error FROM webhook_events ORDER BY received_at DESC LIMIT ?",
            (limit,),
        )
    }


# --------------------------------------------------------------- the dashboard
# Mounted last, deliberately: a catch-all static mount registered earlier would
# shadow every API route defined after it.
STATIC_DIR = Path(__file__).resolve().parent / "static"

if STATIC_DIR.is_dir():
    app.mount("/app", StaticFiles(directory=str(STATIC_DIR), html=True), name="dashboard")

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse(url="/app/")

else:
    @app.get("/")
    def index() -> Dict[str, str]:
        return {
            "service": "rebound",
            "dashboard": "not built - run: cd frontend && npm install && npm run build",
            "health": "/health",
        }
