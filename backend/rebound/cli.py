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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
