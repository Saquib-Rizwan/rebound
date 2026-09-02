"""Command line entry point.

    python -m rebound generate          regenerate the batch
    python -m rebound eval-classifier   score rules vs model vs hybrid
"""
from __future__ import annotations

import argparse
import sys

from . import config
from .diagnose.evaluate import run_variant, write_report
from .sim.drift import build_drift_set
from .sim.generator import load_batch, write_batch


def cmd_generate(args: argparse.Namespace) -> int:
    pay_path, lab_path = write_batch(config.DATA_DIR, n=args.n, seed=args.seed)
    payments, _ = load_batch(config.DATA_DIR)
    at_risk = sum(p.amount for p in payments)
    unstructured = sum(1 for p in payments if p.error_reason is None)
    print("wrote {} and {}".format(pay_path.name, lab_path.name))
    print("  payments      : {}".format(len(payments)))
    print("  at risk       : INR {:,.2f}".format(at_risk / 100))
    print("  unstructured  : {} ({:.0%})".format(unstructured, unstructured / len(payments)))
    return 0


def cmd_eval_classifier(args: argparse.Namespace) -> int:
    payments, labels = load_batch(config.DATA_DIR)
    if args.sample:
        payments = payments[: args.sample]
        labels = {p.payment_id: labels[p.payment_id] for p in payments}
    at_risk = sum(p.amount for p in payments)

    # The model-heavy arms are capped so one evaluation cannot burn a day of free
    # tier. Caps are printed and written into the report - a truncated study that
    # does not say it was truncated reads as full coverage, which is a lie.
    cap = args.model_cap
    model_only_rows = payments[:cap]
    model_only_labels = {p.payment_id: labels[p.payment_id] for p in model_only_rows}

    reports = [
        run_variant("rules_only", payments, labels, use_model=False),
        run_variant("model_only", model_only_rows, model_only_labels,
                    use_model=True, force_all_to_model=True),
        run_variant("hybrid", payments, labels, use_model=True),
    ]
    notes = ["`model_only` was scored on the first {} of {} payments to stay inside the "
             "free-tier quota; every other main-batch arm used all {}.".format(
                 len(model_only_rows), len(payments), len(payments))]

    drift_reports = {}
    if not args.no_drift:
        for mode in ("paraphrase", "noise"):
            d_payments, d_labels = build_drift_set(payments, labels, mode=mode)
            full = len(d_payments)
            d_payments = d_payments[:cap]
            d_labels = {p.payment_id: d_labels[p.payment_id] for p in d_payments}
            notes.append("`{}` drift set scored on {} of {} rows (same rows for both "
                         "arms, so the comparison is like for like).".format(
                             mode, len(d_payments), full))
            drift_reports[mode] = [
                run_variant("rules_only", d_payments, d_labels, use_model=False),
                run_variant("hybrid", d_payments, d_labels, use_model=True),
            ]

    out = write_report(config.REPORTS_DIR / "classifier.md", reports, at_risk,
                       drift_reports, notes=notes)

    print("provider: {}\n".format(reports[-1].provider))
    header = "{:<10} {:<12} {:>9} {:>9} {:>9} {:>13} {:>11} {:>9}".format(
        "set", "arm", "accuracy", "coverage", "macroF1", "error cost", "model rows", "degraded"
    )
    print(header)
    print("-" * len(header))

    def show(set_name, rs):
        for r in rs:
            print("{:<10} {:<12} {:>8.1%} {:>8.1%} {:>9.3f} {:>12,.0f} {:>11} {:>9}".format(
                set_name, r.name, r.accuracy, r.coverage, r.macro_f1,
                r.error_cost_paise / 100, r.model_rows, r.degraded_rows))

    show("main", reports)
    for mode, rs in drift_reports.items():
        show(mode, rs)

    print("\ndeterministic share (hybrid, main): {:.1%}".format(reports[-1].deterministic_share))
    print("report: {}".format(out))
    return 0


def cmd_run(args) -> int:
    """One full agent pass over the batch: diagnose, decide, execute, record."""
    from datetime import timedelta

    from .diagnose.classifier import HybridClassifier
    from .diagnose.llm import OfflineProvider, TailClassifier
    from .execute.executor import Executor
    from .ledger.store import Ledger
    from .policy import engine
    from .policy.guardrails import AgentState

    payments, _ = load_batch(config.DATA_DIR)
    if args.sample:
        payments = payments[: args.sample]

    # Chronological order matters: cooldowns, weekly caps and daily budgets only
    # mean anything if decisions are made in the order the failures happened.
    payments.sort(key=lambda p: p.created_at)

    tail = TailClassifier(OfflineProvider()) if args.provider == "offline" else TailClassifier()
    classifier = HybridClassifier(tail=tail)
    state = AgentState()
    ledger = Ledger(config.DATA_DIR / "rebound.sqlite3")
    executor = Executor(ledger, dry_run=not args.live)

    run_id = args.run_id or "run_{}".format(payments[0].created_at.strftime("%Y%m%d") )
    ledger.start_run(
        run_id=run_id,
        policy_version=config.POLICY_VERSION,
        provider=tail.provider.name,
        model=tail.provider.model,
        dry_run=executor.dry_run,
        batch_size=len(payments),
        notes=args.notes,
    )

    for payment in payments:
        # The agent picks a failure up shortly after it happens, not instantly.
        now = payment.created_at + timedelta(minutes=15)
        diagnosis = classifier.diagnose(payment)
        decision = engine.decide(payment, diagnosis, state, now)
        engine.apply_to_state(decision, state)
        did = ledger.record_decision(run_id, decision)
        executor.execute(decision, did)

    at_risk = sum(p.amount for p in payments)
    print("run: {}   payments: {}   at risk: INR {:,.0f}".format(run_id, len(payments), at_risk / 100))
    print("provider: {} ({})   gateway: {}   dry_run: {}".format(
        tail.provider.name, tail.provider.model, executor.gateway.name, executor.dry_run))
    if tail.degraded_count:
        print("WARNING: {} rows degraded to the offline classifier".format(tail.degraded_count))

    print("\naction mix")
    print("  {:<20} {:>6} {:>16} {:>8}".format("action", "count", "value at risk", "avg P"))
    for row in ledger.action_mix(run_id):
        print("  {:<20} {:>6} {:>16,.0f} {:>8}".format(
            row["intervention"], row["n"], (row["paise"] or 0) / 100, row["avg_p"]))

    print("\nguardrails that blocked something")
    hits = ledger.guardrail_hits(run_id)
    if not hits:
        print("  (none fired - suspicious, check the rails are wired)")
    for row in hits:
        print("  {:<26} blocked {:>5} options across {:>4} payments".format(
            row["guardrail"], row["blocked_candidates"], row["payments"]))

    caution = ledger.cost_of_caution(run_id)
    print("\ncost of caution")
    print("  guardrails suppressed {} payments carrying INR {:,.0f} of expected value".format(
        caution["payments"], caution["forgone_paise"] / 100))
    for row in caution["by_guardrail"]:
        print("    {:<26} INR {:>12,.0f}".format(row["guardrail"], (row["ev_blocked"] or 0) / 100))

    spend = sum(v for (m, d), v in state.spend_by_merchant_day.items())
    contacts = sum(len(v) for v in state.contacts_by_customer.values())
    print("\nspend on actions : INR {:,.2f}".format(spend / 100))
    print("customer contacts: {}".format(contacts))
    print("llm cost         : ${:.4f} over {} calls".format(tail.total_cost_usd, tail.call_count))
    print("ledger           : {}".format(ledger.path))
    ledger.close()
    return 0


def cmd_verify_gateway(args) -> int:
    """Makes exactly ONE real test-mode call, so the integration can be proven.

    Deliberately a separate command rather than a flag on `run`: proving the
    credentials work and blasting a batch at the gateway are different intentions
    and should not share a code path.
    """
    from datetime import datetime

    from .ingest.razorpay_client import MockGateway, RazorpayTestGateway, build_gateway

    gateway = build_gateway()
    print("gateway      : {} (live={})".format(gateway.name, getattr(gateway, "live", False)))

    if isinstance(gateway, MockGateway):
        print("\nNo Razorpay credentials found, so the mock gateway is in use.")
        print("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env to test for real.")
        print("Test-mode keys start with rzp_test_ and move no money.")
        return 1

    if not str(config.RAZORPAY_KEY_ID).startswith("rzp_test_"):
        print("\nREFUSING: key id does not start with rzp_test_.")
        print("This command will not talk to a live Razorpay account.")
        return 2

    stamp = datetime.now().strftime("%H%M%S")
    print("creating one test payment link for INR 499.00 ...")
    result = gateway.create_payment_link(
        payment_id="verify_{}".format(stamp),
        amount_paise=49900,
        description="Rebound integration check - test mode, no money moves",
        expire_in_hours=24.0,
    )
    if result.ok:
        print("\nSUCCESS")
        print("  razorpay id : {}".format(result.reference))
        print("  short url   : {}".format(result.short_url))
        print("\nOpen that URL - it is a real Razorpay checkout page in test mode.")
        print("It will also appear in your dashboard under Payment Links (Test Mode).")
        return 0

    print("\nFAILED: {}".format(result.error))
    return 3


def cmd_eval_policy(args) -> int:
    """Rebound against the naive alternatives, on simulated outcomes."""
    from .diagnose.classifier import HybridClassifier
    from .diagnose.llm import OfflineProvider, TailClassifier
    from .sim import baselines
    from .sim.evaluate_policy import compare, markdown_report, sensitivity, write_report

    payments, truth = load_batch(config.DATA_DIR)
    if args.sample:
        payments = payments[: args.sample]
        truth = {p.payment_id: truth[p.payment_id] for p in payments}
    payments.sort(key=lambda p: p.created_at)

    tail = TailClassifier(OfflineProvider()) if args.provider == "offline" else TailClassifier()
    classifier = HybridClassifier(tail=tail)

    # Diagnose once. Every policy sees the same diagnoses, so any difference
    # between them is the policy, not a different view of the same payment.
    print("diagnosing {} payments with {} ...".format(len(payments), tail.provider.name))
    diagnoses = {p.payment_id: classifier.diagnose(p) for p in payments}
    if tail.degraded_count:
        print("  note: {} rows degraded to the offline classifier".format(tail.degraded_count))

    print("running {} policies x {} replications ...".format(
        len(baselines.POLICY_ORDER), args.replications))
    summaries = compare(payments, diagnoses, truth,
                        replications=args.replications, sigma=args.sigma)

    print("sensitivity sweep ...")
    sens = sensitivity(payments, diagnoses, truth, replications=args.sens_replications)

    report = markdown_report(summaries, sens, args.replications, args.sigma, tail.provider.name)
    out = write_report(config.REPORTS_DIR / "recovery.md", report)

    base = summaries["do_nothing"].mean_net()
    print()
    head = "{:<12} {:>9} {:>14} {:>12} {:>16} {:>10}".format(
        "policy", "rec rate", "recovered", "cost", "net contribution", "contacts")
    print(head)
    print("-" * len(head))
    for name in baselines.POLICY_ORDER:
        s = summaries[name]
        print("{:<12} {:>8.1%} {:>14,.0f} {:>12,.0f} {:>16,.0f} {:>10,.0f}".format(
            name, s.recovery_rate, s.mean("recovered_paise") / 100,
            s.mean("action_cost_paise") / 100, s.mean_net() / 100, s.mean("contacts")))

    agent = summaries["rebound"]
    nothing = summaries["do_nothing"]
    best_naive = max((summaries[n] for n in ("retry_all", "blind_24h", "nudge_all")),
                     key=lambda s: s.mean_net())
    lo, hi = agent.uplift_interval(nothing)
    nlo, nhi = agent.uplift_interval(best_naive)
    print()
    print("uplift vs do_nothing : INR {:>9,.0f}  90% CI [{:,.0f}, {:,.0f}]  wins {:.0%}".format(
        (agent.mean_net() - base) / 100, lo / 100, hi / 100, agent.beats(nothing)))
    print("uplift vs {:<10} : INR {:>9,.0f}  90% CI [{:,.0f}, {:,.0f}]  wins {:.0%}".format(
        best_naive.name, (agent.mean_net() - best_naive.mean_net()) / 100,
        nlo / 100, nhi / 100, agent.beats(best_naive)))
    print("suppressed           : {:.0f} of {} payments".format(
        agent.mean("suppressed"), agent.n_payments))
    print("report               : {}".format(out))
    return 0


def cmd_serve(args) -> int:
    """Runs the webhook receiver and read API."""
    import uvicorn

    print("Rebound API on http://{}:{}".format(args.host, args.port))
    print("  webhook endpoint : POST /webhooks/razorpay")
    print("  health           : GET  /health")
    print("  webhook secret   : {}".format(
        "configured" if config.RAZORPAY_WEBHOOK_SECRET else "MISSING (set RAZORPAY_WEBHOOK_SECRET)"))
    uvicorn.run("rebound.api.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_simulate_webhook(args) -> int:
    """Signs a realistic Razorpay event and posts it to the local receiver.

    Razorpay cannot reach localhost, so without this you would need a tunnel just
    to prove the receiver works. This signs with the same secret the real sender
    uses, so it exercises the genuine verification path rather than bypassing it.
    """
    import json
    import time

    import httpx

    from .ingest.webhooks import compute_signature

    secret = config.RAZORPAY_WEBHOOK_SECRET
    if not secret:
        print("RAZORPAY_WEBHOOK_SECRET is not set. Add it to .env first.")
        return 1

    now = int(time.time())
    if args.event == "payment.failed":
        event = {
            "entity": "event",
            "account_id": "acc_demo",
            "event": "payment.failed",
            "contains": ["payment"],
            "payload": {"payment": {"entity": {
                "id": args.payment_id,
                "entity": "payment",
                "amount": args.amount,
                "currency": "INR",
                "status": "failed",
                "order_id": "order_" + args.payment_id[-8:],
                "method": args.method,
                "bank": "HDFC",
                "error_code": "BAD_REQUEST_ERROR",
                "error_description": args.description,
                "error_source": "bank",
                "error_step": "payment_authorization",
                "error_reason": args.reason or None,
                "created_at": now,
                "notes": {
                    "merchant_id": "mrch_d2c_apparel",
                    "customer_id": "cust_00042",
                    "consent_whatsapp": "true",
                    "consent_sms": "true",
                    "consent_email": "true",
                    "prior_successes": "4",
                },
            }}},
            "created_at": now,
        }
    else:
        event = {
            "entity": "event",
            "account_id": "acc_demo",
            "event": "payment_link.paid",
            "contains": ["payment_link", "payment"],
            "payload": {
                "payment_link": {"entity": {
                    "id": "plink_demo",
                    "reference_id": "rebound_" + args.payment_id,
                    "amount": args.amount,
                    "amount_paid": args.amount,
                    "status": "paid",
                }},
                "payment": {"entity": {
                    "id": args.payment_id,
                    "amount": args.amount,
                    "status": "captured",
                }},
            },
            "created_at": now,
        }

    raw = json.dumps(event, separators=(",", ":")).encode("utf-8")
    signature = compute_signature(raw, secret)
    event_id = args.event_id or "evt_sim_{}_{}".format(args.payment_id[-6:], now)

    url = "http://{}:{}/webhooks/razorpay".format(args.host, args.port)
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature if not args.bad_signature else "deadbeef",
        "X-Razorpay-Event-Id": event_id,
    }
    try:
        resp = httpx.post(url, content=raw, headers=headers, timeout=20.0)
    except Exception as exc:
        print("could not reach {} ({}). Is `python -m rebound serve` running?".format(
            url, type(exc).__name__))
        return 2

    print("POST {} -> {}".format(url, resp.status_code))
    print("  event      : {}".format(event["event"]))
    print("  payment    : {}  INR {:,.2f}".format(args.payment_id, args.amount / 100))
    print("  signature  : {}".format("deliberately invalid" if args.bad_signature else "valid"))
    print("  response   : {}".format(resp.text[:200]))
    return 0 if resp.status_code < 400 else 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rebound")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="regenerate the failed-payment batch")
    gen.add_argument("-n", type=int, default=400)
    gen.add_argument("--seed", type=int, default=7)
    gen.set_defaults(func=cmd_generate)

    ev = sub.add_parser("eval-classifier", help="score the diagnosis layer")
    ev.add_argument("--no-drift", action="store_true", help="skip the held-out drift sets")
    ev.add_argument("--sample", type=int, default=0, help="cap rows for every arm")
    ev.add_argument("--model-cap", type=int, default=150, help="max rows per model-heavy arm")
    ev.set_defaults(func=cmd_eval_classifier)

    run = sub.add_parser("run", help="one full agent pass over the batch")
    run.add_argument("--sample", type=int, default=0)
    run.add_argument("--provider", choices=["auto", "offline"], default="auto")
    run.add_argument("--run-id", default="")
    run.add_argument("--notes", default="")
    run.add_argument("--live", action="store_true",
                     help="actually call the gateway (default is dry run)")
    run.set_defaults(func=cmd_run)

    vg = sub.add_parser("verify-gateway", help="make one real Razorpay test-mode call")
    vg.set_defaults(func=cmd_verify_gateway)

    ep = sub.add_parser("eval-policy", help="rebound vs naive baselines on simulated outcomes")
    ep.add_argument("--sample", type=int, default=0)
    ep.add_argument("--provider", choices=["auto", "offline"], default="offline")
    ep.add_argument("--replications", type=int, default=40)
    ep.add_argument("--sens-replications", type=int, default=12)
    ep.add_argument("--sigma", type=float, default=0.35)
    ep.set_defaults(func=cmd_eval_policy)

    sv = sub.add_parser("serve", help="run the webhook receiver and read API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(func=cmd_serve)

    sw = sub.add_parser("simulate-webhook", help="sign and post a Razorpay event locally")
    sw.add_argument("--event", choices=["payment.failed", "payment_link.paid"],
                    default="payment.failed")
    sw.add_argument("--payment-id", default="pay_DEMO0000001")
    sw.add_argument("--amount", type=int, default=249900)
    sw.add_argument("--method", default="card")
    sw.add_argument("--reason", default="")
    sw.add_argument("--description", default="Declined by bank. Please contact your card issuer.")
    sw.add_argument("--host", default="127.0.0.1")
    sw.add_argument("--port", type=int, default=8000)
    sw.add_argument("--event-id", default="",
                    help="reuse an id to simulate a Razorpay redelivery")
    sw.add_argument("--bad-signature", action="store_true",
                    help="send a wrong signature, to prove it is rejected")
    sw.set_defaults(func=cmd_simulate_webhook)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
