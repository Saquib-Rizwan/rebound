// Three modes, one client.
//   dev     Vite proxies /api to the local backend
//   served  the built app sits behind the same FastAPI process, so no prefix
//   static  no backend at all - flat JSON exported by `rebound.py export-static`,
//           which is what makes a serverless GitHub Pages demo possible
const STATIC = import.meta.env.VITE_STATIC === "1";
const BASE = import.meta.env.DEV ? "/api" : "";

async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} on ${url}`);
  return res.json();
}

const get = (path) => getJson(BASE + path);
const snapshot = (name) => getJson(`api/${name}.json`);

// In static mode a payment's audit trail is looked up in one prefetched map
// rather than fetched per row, which keeps the deploy to a handful of files.
let detailsCache = null;
async function staticDetail(paymentId) {
  if (!detailsCache) detailsCache = await snapshot("details");
  const d = detailsCache[paymentId];
  if (!d) throw new Error("no decision recorded for that payment");
  return d;
}

export const api = STATIC ? {
  health: () => snapshot("health"),
  runs: () => snapshot("runs"),
  summary: () => snapshot("summary"),
  decisions: () => snapshot("decisions"),
  decision: staticDetail,
  webhooks: () => snapshot("webhooks"),
  recovery: () => snapshot("recovery"),
  insights: () => snapshot("insights"),
  scheduled: () => snapshot("scheduled"),
} : {
  health: () => get("/health"),
  runs: () => get("/runs"),
  summary: (runId) => get(`/summary?run_id=${encodeURIComponent(runId)}`),
  decisions: (runId, intervention = "") =>
    get(`/decisions?run_id=${encodeURIComponent(runId)}&limit=500` +
        (intervention ? `&intervention=${intervention}` : "")),
  decision: (paymentId) => get(`/decisions/${encodeURIComponent(paymentId)}`),
  webhooks: () => get("/webhooks/recent?limit=12"),
  recovery: () => get("/reports/recovery"),
  insights: () => get("/insights"),
  scheduled: () => get("/scheduled"),
};

export const isStatic = STATIC;

export const rupees = (paise, digits = 0) =>
  "₹" + (Number(paise || 0) / 100).toLocaleString("en-IN", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });

export const pct = (x, digits = 1) => `${(Number(x || 0) * 100).toFixed(digits)}%`;
