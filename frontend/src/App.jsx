import { useEffect, useMemo, useState } from "react";
import { api, pct, rupees } from "./api";

const ACTIONS = [
  "", "retry_scheduled", "retry_now", "nudge_link",
  "switch_rail", "request_remandate", "suppress", "escalate_human",
];

function Kpi({ label, value, sub, good }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={"value" + (good ? " good" : "")}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

function PolicyBars({ data }) {
  if (!data) return <div className="empty">Run <code>python rebound.py eval-policy</code> to populate this.</div>;
  const max = Math.max(...data.policies.map((p) => p.net_paise));
  const h = data.headline;
  return (
    <>
      <div className="bars">
        {data.policies.map((p) => (
          <div key={p.name}>
            <div className={"bar-row" + (p.name === "rebound" ? " me" : "")}>
              <div className="bar-name">{p.name}</div>
              <div className="bar-track">
                <div className="bar-fill" style={{ width: `${Math.max(2, (p.net_paise / max) * 100)}%` }} />
              </div>
              <div className="bar-val">{rupees(p.net_paise)}</div>
            </div>
            <div className="bar-meta">
              {pct(p.recovery_rate)} recovered · {Math.round(p.contacts)} contacts
              {p.churned > 0.5 ? ` · ${p.churned.toFixed(1)} churned` : " · 0 churned"}
            </div>
          </div>
        ))}
      </div>
      <div className="foot" style={{ marginTop: 18 }}>
        Net contribution after costs, over {data.meta.replications} replications on identical
        random draws. Against doing nothing: <b style={{ color: "var(--good)" }}>
        +{rupees(h.uplift_vs_nothing_paise)}</b> (90% CI {rupees(h.uplift_ci_low_paise)} to{" "}
        {rupees(h.uplift_ci_high_paise)}, won {pct(h.win_rate_vs_nothing, 0)} of runs).
        <br />
        Against <code>{h.best_naive}</code>: +{rupees(h.uplift_vs_best_naive_paise)} — won{" "}
        {pct(h.win_rate_vs_best_naive, 0)} of runs, but the interval crosses zero. The agent is
        not richer than messaging everyone; it is quieter.
      </div>
    </>
  );
}

function DecisionDetail({ paymentId }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!paymentId) { setD(null); return; }
    setD(null); setErr(null);
    api.decision(paymentId).then(setD).catch((e) => setErr(e.message));
  }, [paymentId]);

  if (!paymentId) return <div className="empty">Select a payment to see why the agent decided what it did.</div>;
  if (err) return <div className="empty">{err}</div>;
  if (!d) return <div className="empty">Loading…</div>;

  const dec = d.decision;
  return (
    <div className="body">
      <div className="reason">{dec.explanation}</div>
      <dl className="kv">
        <dt>Payment</dt><dd style={{ fontFamily: "var(--mono)" }}>{dec.payment_id}</dd>
        <dt>Amount</dt><dd>{rupees(dec.amount_paise, 2)}</dd>
        <dt>Root cause</dt>
        <dd>{dec.failure_class} <span className={"src " + (dec.diag_source || "").split("+")[0]}>
          ({pct(dec.confidence, 0)} confidence via {dec.diag_source})</span></dd>
        <dt>Decision</dt><dd><span className={"chip " + dec.intervention}>{dec.intervention}</span></dd>
        {dec.rationale && <><dt>Reasoning</dt><dd style={{ color: "var(--muted)" }}>{dec.rationale}</dd></>}
      </dl>

      <div className="label" style={{ font: "500 11px var(--mono)", color: "var(--dim)", textTransform: "uppercase", letterSpacing: ".07em", marginBottom: 8 }}>
        Every option it priced
      </div>
      <table>
        <thead>
          <tr>
            <th>Action</th><th className="num">Delay</th><th className="num">P(rec)</th>
            <th className="num">EV</th><th>Verdict</th>
          </tr>
        </thead>
        <tbody>
          {d.considered.map((c, i) => (
            <tr key={i} style={{ cursor: "default" }}>
              <td>{c.chosen ? <span className="chosen-mark">▸ </span> : ""}{c.intervention}
                  {c.channel !== "none" && <span className="src"> · {c.channel}</span>}</td>
              <td className="num">{c.delay_hours ? `${c.delay_hours}h` : "—"}</td>
              <td className="num">{c.p_recover ? c.p_recover.toFixed(2) : "—"}</td>
              <td className="num">{rupees(c.ev_paise)}</td>
              <td>{c.blocked_by
                ? <span className="blocked">{c.blocked_by}</span>
                : <span className="allowed">{c.chosen ? "chosen" : "allowed"}</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {d.outcome?.length > 0 && (
        <div className="foot">
          Outcome: <b style={{ color: "var(--good)" }}>recovered {rupees(d.outcome[0].recovered_paise)}</b>
          {" "}· source <code>{d.outcome[0].source}</code>
          {d.outcome[0].source === "observed" && " — measured, not simulated."}
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [health, setHealth] = useState(null);
  const [runs, setRuns] = useState([]);
  const [runId, setRunId] = useState("demo");
  const [summary, setSummary] = useState(null);
  const [rows, setRows] = useState([]);
  const [filter, setFilter] = useState("");
  const [sel, setSel] = useState(null);
  const [recovery, setRecovery] = useState(null);
  const [hooks, setHooks] = useState([]);

  useEffect(() => {
    api.health().then(setHealth).catch(() => {});
    api.recovery().then(setRecovery).catch(() => setRecovery(null));
    api.runs().then((d) => {
      setRuns(d.runs || []);
      const withRows = (d.runs || []).find((r) => r.batch_size > 0);
      if (withRows) setRunId(withRows.run_id);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!runId) return;
    api.summary(runId).then(setSummary).catch(() => setSummary(null));
    api.decisions(runId, filter).then((d) => setRows(d.decisions || [])).catch(() => setRows([]));
    api.webhooks().then((d) => setHooks(d.events || [])).catch(() => {});
  }, [runId, filter]);

  const totals = useMemo(() => {
    const atRisk = rows.reduce((a, r) => a + r.amount_paise, 0);
    const suppressed = rows.filter((r) => r.intervention === "suppress").length;
    const contacted = rows.filter((r) =>
      ["nudge_link", "switch_rail", "request_remandate"].includes(r.intervention)).length;
    return { atRisk, suppressed, contacted, n: rows.length };
  }, [rows]);

  const caution = summary?.cost_of_caution;
  const observed = (summary?.outcomes || []).find((o) => o.source === "observed");

  return (
    <div className="wrap">
      <header className="top">
        <h1>Rebound</h1>
        <span className="tag">payment recovery agent · Razorpay AI Buildathon, Track 03</span>
        <span className="spacer" />
        {health && <>
          <span className={"pill " + (health.dry_run ? "dry" : "live")}>
            {health.dry_run ? "dry run" : "live execution"}</span>
          <span className="pill">{health.provider}</span>
          <span className="pill">{health.policy_version}</span>
        </>}
      </header>

      <div className="banner">
        <b>Simulated outcomes.</b> Every recovery figure below comes from a model that is
        deliberately built to disagree with the agent&rsquo;s own beliefs — no real payment was
        recovered. Recoveries confirmed by a real Razorpay webhook are stored separately and
        labelled <code>observed</code>.
      </div>

      <div className="grid kpis">
        <Kpi label="Payments" value={totals.n} sub={`${rupees(totals.atRisk)} at risk`} />
        <Kpi label="Net uplift" good
             value={recovery ? `+${rupees(recovery.headline.uplift_vs_nothing_paise)}` : "—"}
             sub={recovery ? `vs doing nothing · won ${pct(recovery.headline.win_rate_vs_nothing, 0)} of runs` : "run eval-policy"} />
        <Kpi label="Chose silence" value={totals.suppressed}
             sub={totals.n ? `${pct(totals.suppressed / totals.n, 0)} of payments, deliberately` : ""} />
        <Kpi label="Customers contacted" value={totals.contacted}
             sub={`out of ${totals.n} failures`} />
        <Kpi label="Cost of caution"
             value={caution ? rupees(caution.forgone_paise) : "—"}
             sub={caution ? `expected value the guardrails refused` : ""} />
        <Kpi label="Observed recoveries"
             value={observed ? rupees(observed.paise) : "₹0"}
             sub="confirmed by real Razorpay webhooks" />
      </div>

      <div className="cols">
        <div className="panel">
          <h2>Decisions <small>{runId} · click a row for the full reasoning</small></h2>
          <div className="filters">
            {ACTIONS.map((a) => (
              <button key={a || "all"} className={filter === a ? "on" : ""} onClick={() => setFilter(a)}>
                {a || "all"}
              </button>
            ))}
          </div>
          <div className="body scroll" style={{ paddingTop: 12 }}>
            {rows.length === 0
              ? <div className="empty">No decisions for this run. Try <code>python rebound.py run --run-id demo</code></div>
              : <table>
                  <thead>
                    <tr>
                      <th>Payment</th><th className="num">Amount</th>
                      <th>Root cause</th><th>Decision</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.payment_id + r.decided_at}
                          className={sel === r.payment_id ? "sel" : ""}
                          onClick={() => setSel(r.payment_id)}>
                        <td style={{ fontFamily: "var(--mono)", fontSize: 11.5 }}>{r.payment_id}</td>
                        <td className="num">{rupees(r.amount_paise)}</td>
                        <td>{r.failure_class}
                          <div className={"src " + (r.diag_source || "").split("+")[0]}>
                            {pct(r.confidence, 0)} · {r.diag_source}</div></td>
                        <td><span className={"chip " + r.intervention}>{r.intervention}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>}
          </div>
        </div>

        <div style={{ display: "grid", gap: 14 }}>
          <div className="panel">
            <h2>Why <small>the audit trail</small></h2>
            <DecisionDetail paymentId={sel} />
          </div>

          <div className="panel">
            <h2>Net contribution by policy <small>simulated</small></h2>
            <div className="body"><PolicyBars data={recovery} /></div>
          </div>

          <div className="panel">
            <h2>Guardrails that blocked something</h2>
            <div className="body">
              {(summary?.guardrails || []).length === 0
                ? <div className="empty">None fired — suspicious.</div>
                : <>
                    <div className="guard-row" style={{ color: "var(--dim)", fontSize: 11 }}>
                      <div>rail</div><div className="num">options</div><div className="num">payments</div>
                    </div>
                    {summary.guardrails.map((g) => (
                      <div className="guard-row" key={g.guardrail}>
                        <div className="gname">{g.guardrail}</div>
                        <div className="num">{g.blocked_candidates}</div>
                        <div className="num">{g.payments}</div>
                      </div>
                    ))}
                  </>}
            </div>
          </div>

          <div className="panel">
            <h2>Webhook receipts <small>signature verified</small></h2>
            <div className="body">
              {hooks.length === 0 ? <div className="empty">No webhooks received yet.</div>
                : hooks.slice(0, 8).map((h) => (
                  <div className="guard-row" key={h.event_id} style={{ gridTemplateColumns: "1fr 96px" }}>
                    <div className="gname">
                      {h.event_type}
                      <div className="src" style={{ color: h.error ? "var(--warn)" : "var(--dim)" }}>
                        {h.error || h.payment_id || ""}</div>
                    </div>
                    <div className="num" style={{ color: h.signature_ok ? "var(--good)" : "var(--bad)" }}>
                      {h.signature_ok ? "verified" : "REJECTED"}
                    </div>
                  </div>
                ))}
            </div>
          </div>
        </div>
      </div>

      <div className="foot">
        Runs available: {runs.map((r) => (
          <button key={r.run_id} onClick={() => setRunId(r.run_id)}
                  style={{ background: "none", border: "none", color: r.run_id === runId ? "var(--accent)" : "var(--dim)",
                           cursor: "pointer", font: "12px var(--mono)", padding: "0 6px" }}>
            {r.run_id}
          </button>
        ))}
      </div>
    </div>
  );
}
