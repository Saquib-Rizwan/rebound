// In dev, Vite proxies /api to the backend. In the built app the API is the same
// origin, so the prefix is empty. One switch, no environment files.
const BASE = import.meta.env.DEV ? "/api" : "";

async function get(path) {
  const res = await fetch(BASE + path);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} on ${path}`);
  return res.json();
}

export const api = {
  health: () => get("/health"),
  runs: () => get("/runs"),
  summary: (runId) => get(`/summary?run_id=${encodeURIComponent(runId)}`),
  decisions: (runId, intervention = "") =>
    get(`/decisions?run_id=${encodeURIComponent(runId)}&limit=500` +
        (intervention ? `&intervention=${intervention}` : "")),
  decision: (paymentId) => get(`/decisions/${encodeURIComponent(paymentId)}`),
  webhooks: () => get("/webhooks/recent?limit=12"),
  recovery: () => get("/reports/recovery"),
};

export const rupees = (paise, digits = 0) =>
  "₹" + (Number(paise || 0) / 100).toLocaleString("en-IN", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });

export const pct = (x, digits = 1) => `${(Number(x || 0) * 100).toFixed(digits)}%`;
