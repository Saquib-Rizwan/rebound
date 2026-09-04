import { useEffect, useState } from "react";
import { api, pct, rupees } from "./api";
import * as L from "./labels";

const FILTERS = [
  "", "retry_scheduled", "retry_now", "nudge_link",
  "switch_rail", "request_remandate", "suppress", "escalate_human",
];

function sourceClass(src) {
  return (src || "").split("+")[0];
}

/* ───────────────────────────────────────────── decisions */

function Why({ paymentId }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!paymentId) { setD(null); return; }
    setD(null); setErr(null);
    api.decision(paymentId).then(setD).catch((e) => setErr(e.message));
  }, [paymentId]);

  if (!paymentId) {
    return (
      <div className="blank">
        Select a payment to see the full audit trail —<br />
        every option the agent priced, and the guardrail that stopped each one it skipped.
      </div>
    );
  }
  if (err) return <div className="blank">{err}</div>;
  if (!d) return <div className="blank" style={{ padding: "24px 20px" }}>Loading…</div>;

  const dec = d.decision;
  return (
    <div className="pad">
      <div className="quote">{dec.explanation}</div>

      <dl className="facts">
        <dt>Payment</dt><dd className="id">{dec.payment_id}</dd>
        <dt>Amount</dt><dd>{rupees(dec.amount_paise, 2)}</dd>
        <dt>Root cause</dt>
        <dd>
          <span title={dec.failure_class}>{L.cause(dec.failure_class)}</span>
          <div className="by">
            {pct(dec.confidence, 0)} confidence · decided by{" "}
            <span className={sourceClass(dec.diag_source)}>{L.source(dec.diag_source)}</span>
          </div>
        </dd>
        <dt>Decision</dt>
        <dd>
          <span className={"tag " + dec.intervention} title={dec.intervention}>
            {L.action(dec.intervention)}
          </span>
        </dd>
        {dec.rationale && (
          <><dt>Reasoning</dt><dd style={{ color: "var(--muted)", fontSize: 12 }}>{dec.rationale}</dd></>
        )}
      </dl>

      <div className="mini-h">Every option it priced</div>
      <table>
        <thead>
          <tr>
            <th>Action</th><th className="num">Delay</th>
            <th className="num">P(rec)</th><th className="num">EV</th><th>Verdict</th>
          </tr>
        </thead>
        <tbody>
          {d.considered.map((c, i) => (
            <tr key={i}>
              <td>
                <span title={c.intervention}>{L.action(c.intervention)}</span>
                {c.channel && c.channel !== "none" && (
                  <span className="by"> via {L.channel(c.channel)}</span>
                )}
              </td>
              <td className="num">{c.delay_hours ? `${c.delay_hours}h` : "—"}</td>
              <td className="num">{c.p_recover ? c.p_recover.toFixed(2) : "—"}</td>
              <td className="num">{rupees(c.ev_paise)}</td>
              <td>
                {c.blocked_by
                  ? <span className="verdict-block" title={c.blocked_by}>
                      {L.guardrail(c.blocked_by)}
                    </span>
                  : c.chosen
                    ? <span className="verdict-ok">chosen</span>
                    : <span className="verdict-idle">allowed</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {d.outcome?.length > 0 && (
        <div className="note">
          <b>Outcome:</b> recovered {rupees(d.outcome[0].recovered_paise)} · source{" "}
          <code>{d.outcome[0].source}</code>
          {d.outcome[0].source === "observed" && " — measured by a real webhook, not simulated."}
        </div>
      )}
    </div>
  );
}

export function Decisions({ runId, rows }) {
  const [filter, setFilter] = useState("");
  const [sel, setSel] = useState(null);
  const shown = filter ? rows.filter((r) => r.intervention === filter) : rows;

  // Select the largest payment automatically. The audit trail is the most
  // interesting thing here and an empty panel on first load hides it behind a
  // click nobody knows to make.
  useEffect(() => {
    if (shown.length && !shown.some((r) => r.payment_id === sel)) {
      setSel(shown[0].payment_id);
    }
  }, [shown, sel]);

  return (
    <div className="split">
      <div className="panel">
        <header>
          <h3>Decisions</h3>
          <small>{runId} · click any row</small>
        </header>
        <div className="chips">
          {FILTERS.map((f) => (
            <button key={f || "all"} className={filter === f ? "on" : ""}
                    title={f || "all actions"} onClick={() => setFilter(f)}>
              {f ? L.action(f) : "All"}
            </button>
          ))}
        </div>
        <div className="scroll scroll-fade">
          {shown.length === 0
            ? <div className="blank">Nothing here. Run <code>python rebound.py run --run-id demo</code></div>
            : (
              <table>
                <thead>
                  <tr>
                    <th>Payment</th><th className="num">Amount</th>
                    <th>Root cause</th><th>Decision</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((r) => (
                    <tr key={r.payment_id + r.decided_at}
                        className={"click" + (sel === r.payment_id ? " on" : "")}
                        onClick={() => setSel(r.payment_id)}>
                      <td className="id">{r.payment_id}</td>
                      <td className="num">{rupees(r.amount_paise)}</td>
                      <td>
                        <span title={r.failure_class}>{L.cause(r.failure_class)}</span>
                        <div className="by">
                          {pct(r.confidence, 0)} ·{" "}
                          <span className={sourceClass(r.diag_source)}>
                            {L.source(r.diag_source)}
                          </span>
                        </div>
                      </td>
                      <td>
                        <span className={"tag " + r.intervention} title={r.intervention}>
                          {L.action(r.intervention)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
        </div>
      </div>

      <div className="panel">
        <header><h3>Why</h3><small>the audit trail</small></header>
        <Why paymentId={sel} />
      </div>
    </div>
  );
}

/* ───────────────────────────────────────────── insights */

export function Insights({ insights }) {
  if (!insights) {
    return (
      <div className="panel">
        <div className="blank">
          Run <code>python rebound.py insights</code> to generate systemic findings.
        </div>
      </div>
    );
  }
  return (
    <div>
      <div className="note" style={{ marginTop: 0, marginBottom: 16 }}>
        Per-payment recovery treats symptoms. These are the patterns underneath, ranked by
        money at stake. Detection is deterministic grouping and thresholds — <b>no model
        invents a recommendation or a number.</b>
      </div>
      {insights.map((i, n) => (
        <div className="insight" key={n}>
          <div className="i-top">
            <h4>{i.title}</h4>
            <span className={"sev " + i.severity}>{i.severity}</span>
            <span className="grow" style={{ flex: 1 }} />
            <span className="i-val">{rupees(i.value_paise)}</span>
          </div>
          <p>{i.detail}</p>
          <div className="do"><b>Do this: </b>{i.recommendation}</div>
        </div>
      ))}
    </div>
  );
}

/* ───────────────────────────────────────────── results */

export function Results({ recovery }) {
  if (!recovery) {
    return (
      <div className="panel">
        <div className="blank">
          Run <code>python rebound.py eval-policy --replications 40</code> first.
        </div>
      </div>
    );
  }
  const max = Math.max(...recovery.policies.map((p) => p.net_paise));
  const h = recovery.headline;

  return (
    <div className="split">
      <div className="panel">
        <header>
          <h3>Net contribution by policy</h3>
          <small>{recovery.meta.replications} replications, paired</small>
        </header>
        <div className="pad">
          <div className="bars">
            {recovery.policies.map((p) => (
              <div className={"bar-row" + (p.name === "rebound" ? " me" : "")} key={p.name}>
                <div className="bar-head">
                  <span className="bar-label">{p.name}</span>
                  <span className="bar-amt">{rupees(p.net_paise)}</span>
                </div>
                <div className="bar-track">
                  <div className="bar-fill" style={{ width: `${Math.max(1.5, (p.net_paise / max) * 100)}%` }} />
                </div>
                <div className="bar-note">
                  {pct(p.recovery_rate)} recovered · {Math.round(p.contacts)} contacts ·{" "}
                  {p.churned > 0.5 ? `${p.churned.toFixed(1)} churned` : "0 churned"}
                </div>
              </div>
            ))}
          </div>
          <div className="note">
            <b>Against doing nothing:</b> +{rupees(h.uplift_vs_nothing_paise)} (90% CI{" "}
            {rupees(h.uplift_ci_low_paise)} to {rupees(h.uplift_ci_high_paise)}), won{" "}
            {pct(h.win_rate_vs_nothing, 0)} of runs.
            <br /><br />
            <b>Against <code>{h.best_naive}</code>:</b> +{rupees(h.uplift_vs_best_naive_paise)},
            won {pct(h.win_rate_vs_best_naive, 0)} of runs — but the interval crosses zero.
            The agent is <i>not</i> significantly richer than messaging everyone. It is
            quieter, and it churns nobody.
          </div>
        </div>
      </div>

      <div className="panel">
        <header>
          <h3>How wrong can it be and still win?</h3>
          <small>sensitivity</small>
        </header>
        <div className="pad">
          <table>
            <thead>
              <tr>
                <th>σ</th><th className="num">do nothing</th>
                <th className="num">nudge all</th><th className="num">rebound</th><th>rank</th>
              </tr>
            </thead>
            <tbody>
              {recovery.sensitivity.map((s) => (
                <tr key={s.sigma}>
                  <td className="num">{s.sigma}</td>
                  <td className="num">{rupees(s.net.do_nothing)}</td>
                  <td className="num">{rupees(s.net.nudge_all)}</td>
                  <td className="num" style={{ color: s.agent_rank === 1 ? "var(--good)" : "var(--warn)" }}>
                    {rupees(s.net.rebound)}
                  </td>
                  <td className={s.agent_rank === 1 ? "verdict-ok" : "verdict-block"}>
                    {s.agent_rank} of 5
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="note">
            σ is how badly the agent&rsquo;s efficacy beliefs are wrong. It wins while they are
            roughly right, and <b>loses from σ 0.5</b> — a policy that targets badly is worse
            than one that does not target at all. That is the honest boundary of this
            approach, and the argument for calibrating against observed outcomes before
            trusting it with a budget.
          </div>
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────────────────────────── operations */

export function Operations({ summary, observed }) {
  const [sched, setSched] = useState(null);
  const [hooks, setHooks] = useState([]);

  useEffect(() => {
    api.scheduled().then(setSched).catch(() => setSched(null));
    api.webhooks().then((d) => setHooks(d.events || [])).catch(() => {});
  }, []);

  return (
    <div className="split">
      <div style={{ display: "grid", gap: 16 }}>
        <div className="panel">
          <header><h3>Guardrails that blocked something</h3></header>
          <div className="pad">
            {(summary?.guardrails || []).length === 0
              ? <div className="blank">None fired — suspicious.</div>
              : summary.guardrails.map((g) => (
                  <div className="row2" key={g.guardrail}>
                    <span className="r-name" title={g.guardrail}>{L.guardrail(g.guardrail)}</span>
                    <span className="r-n">{g.blocked_candidates}</span>
                    <span className="r-n" style={{ color: "var(--dim)" }}>{g.payments}</span>
                  </div>
                ))}
            <div className="note">Options blocked · payments affected.</div>
          </div>
        </div>

        <div className="panel">
          <header><h3>Scheduled actions</h3><small>promised for later</small></header>
          <div className="pad">
            {!sched ? <div className="blank">—</div> : <>
              {sched.pending.length > 0 && <>
                <div className="mini-h" style={{ marginTop: 0 }}>Still queued</div>
                {sched.pending.map((p) => (
                  <div className="row2" key={p.intervention}>
                    <span className="r-name" title={p.intervention}>{L.action(p.intervention)}</span>
                    <span className="r-n">{p.n}</span>
                    <span className="r-n" style={{ color: "var(--dim)", fontSize: 10 }}>
                      {(p.next_due || "").slice(5, 16)}
                    </span>
                  </div>
                ))}
              </>}
              {sched.fired.length > 0 && <>
                <div className="mini-h">Fired</div>
                {sched.fired.map((f) => (
                  <div className="row2" key={f.fire_result}>
                    <span className={f.fire_result?.startsWith("cancelled") ? "err" : "ok"}>
                      {f.fire_result}
                    </span>
                    <span className="r-n">{f.n}</span>
                    <span />
                  </div>
                ))}
              </>}
              <div className="note">
                Guardrails are <b>re-checked at fire time</b>, not trusted from when the
                decision was made. A payment recovered in the meantime cancels its own
                follow-up.
              </div>
            </>}
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gap: 16 }}>
        <div className="panel">
          <header><h3>Webhook receipts</h3><small>HMAC verified</small></header>
          <div className="pad scroll" style={{ maxHeight: 400 }}>
            {hooks.length === 0 ? <div className="blank">No webhooks received yet.</div>
              : hooks.map((h) => (
                <div className="row2" key={h.event_id}>
                  <span>
                    <span className="r-name">{h.event_type}</span>
                    <div className="by" style={{ color: h.error ? "var(--warn)" : "var(--faint)" }}>
                      {h.error || h.payment_id || h.event_id}
                    </div>
                  </span>
                  <span className={h.signature_ok ? "ok" : "err"}>
                    {h.signature_ok ? "verified" : "REJECTED"}
                  </span>
                  <span />
                </div>
              ))}
          </div>
        </div>

        <div className="panel">
          <header><h3>Outcomes</h3><small>measured vs modelled</small></header>
          <div className="pad">
            {(summary?.outcomes || []).map((o) => (
              <div className="row2" key={o.source}>
                <span className="r-name">{o.source}</span>
                <span className="r-n">{o.recovered}</span>
                <span className="r-n">{rupees(o.paise)}</span>
              </div>
            ))}
            <div className="note">
              {observed
                ? <><b>{rupees(observed.paise)}</b> confirmed by real Razorpay webhooks. Every
                   other figure in this dashboard is simulated.</>
                : "No observed recoveries yet — pay a recovery link in Razorpay test mode."}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
