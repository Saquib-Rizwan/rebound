// Human labels for internal identifiers.
//
// `auth_dropoff` and `retry_scheduled` are enum values. They are correct in the
// database, the API and the audit trail - stable machine identifiers are the whole
// point of a closed vocabulary. They just should not be what a merchant reads.
//
// Every raw value stays available as a `title` tooltip, so an engineer debugging
// against the ledger can still map what they see back to what is stored.

export const CAUSE = {
  insufficient_funds: "Insufficient funds",
  bank_downtime: "Bank downtime",
  auth_dropoff: "Checkout drop-off",
  expired_instrument: "Expired card",
  invalid_instrument: "Invalid account",
  limit_exceeded: "Limit exceeded",
  risk_decline_issuer: "Declined by bank",
  suspected_fraud: "Suspected fraud",
  mandate_inactive: "Mandate inactive",
  technical_error: "Technical error",
  customer_cancelled: "Customer cancelled",
  unknown: "Cause unclear",
};

export const ACTION = {
  retry_now: "Retry now",
  retry_scheduled: "Retry later",
  nudge_link: "Send payment link",
  switch_rail: "Offer another method",
  request_remandate: "Re-authorise mandate",
  escalate_human: "Send to a human",
  suppress: "Stay silent",
};

export const CHANNEL = {
  whatsapp: "WhatsApp",
  sms: "SMS",
  email: "email",
  none: "",
};

export const SOURCE = {
  rules: "rules",
  gemini: "AI model",
  anthropic: "AI model",
  offline_tfidf: "offline classifier",
  rules_abstain: "rules (abstained)",
};

// Plain-English rendering of the guardrail that blocked an option. The id is kept
// alongside because it is what appears in the ledger and in the tests.
export const GUARDRAIL = {
  G00_kill_switch: "Kill switch engaged",
  G01_never_retry_class: "This cause must never be retried",
  G02_never_contact_class: "This cause must never be messaged",
  G03_max_attempts: "Attempt limit reached",
  G04_no_channel: "No channel available",
  G04_channel_consent: "No consent on this channel",
  G05_quiet_hours: "Would arrive during quiet hours",
  G06_contact_cooldown: "Customer contacted too recently",
  G07_frequency_cap: "Weekly contact cap reached",
  G08_min_ticket: "Ticket too small to interrupt for",
  G09_daily_budget: "Merchant daily budget reached",
  G10_unknown_class: "Cause unclear — action not permitted",
  G11_pre_debit_notice: "RBI: needs 24h pre-debit notice",
  G12_afa_required: "RBI: needs customer re-authentication",
  OVR_high_value_unknown: "High value and unexplained — sent to a human",
};

const humanise = (value) =>
  (value || "").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

export const cause = (v) => CAUSE[v] || humanise(v);
export const action = (v) => ACTION[v] || humanise(v);
export const channel = (v) => CHANNEL[v] ?? v;
export const source = (v) => {
  // sources can be suffixed, e.g. "gemini+sanitized"
  const [base, ...rest] = (v || "").split("+");
  const label = SOURCE[base] || humanise(base);
  return rest.length ? `${label} (sanitised)` : label;
};
export const guardrail = (v) => GUARDRAIL[v] || humanise(v);
