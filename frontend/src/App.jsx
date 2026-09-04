import { useEffect, useMemo, useState } from "react";
import { api, pct, rupees } from "./api";
import { Decisions, Insights, Operations, Results } from "./tabs";

const TABS = [
  ["decisions", "Decisions"],
  ["insights", "What to fix"],
  ["results", "Results"],
  ["ops", "Operations"],
];

function Kpi({ label, value, sub, positive }) {
  return (
    <div className="kpi">
      <div className="k-label">{label}</div>
      <div className={"k-value" + (positive ? " pos" : "")}>{value}</div>
      {sub && <div className="k-sub">{sub}</div>}
    </div>
  );
}

export default function App() {
  // Tabs live in the URL hash so a specific view can be linked to directly -
  // useful for a demo that needs to jump straight to the results.
  const [tab, setTab] = useState(
    () => (window.location.hash || "").replace("#", "") || "decisions"
  );
  useEffect(() => {
    const onHash = () =>
      setTab((window.location.hash || "").replace("#", "") || "decisions");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const [health, setHealth] = useState(null);
  const [runs, setRuns] = useState([]);
  const [runId, setRunId] = useState("");
  const [summary, setSummary] = useState(null);
  const [rows, setRows] = useState([]);
  const [recovery, setRecovery] = useState(null);
  const [insights, setInsights] = useState(null);

  useEffect(() => {
    api.health().then(setHealth).catch(() => {});
    api.recovery().then(setRecovery).catch(() => setRecovery(null));
    api.insights().then((d) => setInsights(d.insights)).catch(() => setInsights(null));
    api.runs().then((d) => {
      const list = d.runs || [];
      setRuns(list);
      const withRows = list.find((r) => r.batch_size > 0);
      setRunId(withRows ? withRows.run_id : (list[0]?.run_id || ""));
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!runId) return;
    api.summary(runId).then(setSummary).catch(() => setSummary(null));
    api.decisions(runId).then((d) => setRows(d.decisions || [])).catch(() => setRows([]));
  }, [runId]);

  const totals = useMemo(() => {
    const atRisk = rows.reduce((a, r) => a + r.amount_paise, 0);
    const suppressed = rows.filter((r) => r.intervention === "suppress").length;
    const contacted = rows.filter((r) =>
      ["nudge_link", "switch_rail", "request_remandate"].includes(r.intervention)).length;
    const byModel = rows.filter((r) => !(r.diag_source || "").startsWith("rules")).length;
    return { atRisk, suppressed, contacted, byModel, n: rows.length };
  }, [rows]);

  const caution = summary?.cost_of_caution;
  const observed = (summary?.outcomes || []).find((o) => o.source === "observed");

  return (
    <div className="wrap">
      <div className="masthead">
        <span className="mark">Rebound<span className="dot">.</span></span>
        <span className="mark-sub">payment recovery agent</span>
        <span className="grow" />
        {health && <>
          <span className={"pill " + (health.dry_run ? "dry" : "live")}>
            {health.dry_run ? "dry run" : "live execution"}
          </span>
          <span className="pill">{health.provider}</span>
          <span className="pill">{health.policy_version}</span>
        </>}
      </div>

      <div className="disclose">
        <div>
          <b>Simulated outcomes.</b> Every recovery figure here comes from a world model
          built to <i>disagree</i> with the agent&rsquo;s own beliefs — no real payment was
          recovered. Recoveries confirmed by a real Razorpay webhook are stored separately
          and labelled <code>observed</code>. Nothing in this project merges the two.
        </div>
      </div>

      <div className="kpis">
        <Kpi label="Failed payments" value={totals.n}
             sub={`${rupees(totals.atRisk)} at risk`} />
        <Kpi label="Net uplift" positive
             value={recovery ? `+${rupees(recovery.headline.uplift_vs_nothing_paise)}` : "—"}
             sub={recovery
               ? `vs doing nothing · won ${pct(recovery.headline.win_rate_vs_nothing, 0)} of runs`
               : "run eval-policy"} />
        <Kpi label="Chose silence" value={totals.suppressed}
             sub={totals.n ? `${pct(totals.suppressed / totals.n, 0)} of payments, deliberately` : ""} />
        <Kpi label="Customers contacted" value={totals.contacted}
             sub={`of ${totals.n} failures`} />
        <Kpi label="Needed the model" value={totals.byModel}
             sub={totals.n ? `${pct(totals.byModel / totals.n, 0)} — rules handled the rest free` : ""} />
        <Kpi label="Cost of caution"
             value={caution ? rupees(caution.forgone_paise) : "—"}
             sub="value the guardrails refused" />
      </div>

      <div className="tabs">
        {TABS.map(([key, label]) => (
          <button key={key} className={tab === key ? "on" : ""}
                  onClick={() => { window.location.hash = key; setTab(key); }}>
            {label}
            {key === "decisions" && rows.length > 0 && <span className="count">{rows.length}</span>}
            {key === "insights" && insights && <span className="count">{insights.length}</span>}
          </button>
        ))}
      </div>

      {tab === "decisions" && <Decisions runId={runId} rows={rows} />}
      {tab === "insights" && <Insights insights={insights} />}
      {tab === "results" && <Results recovery={recovery} />}
      {tab === "ops" && <Operations summary={summary} observed={observed} />}

      <footer className="foot">
        <span>run</span>
        {runs.map((r) => (
          <button key={r.run_id} className={r.run_id === runId ? "on" : ""}
                  onClick={() => setRunId(r.run_id)}>
            {r.run_id}{r.batch_size ? ` · ${r.batch_size}` : ""}
          </button>
        ))}
      </footer>
    </div>
  );
}
