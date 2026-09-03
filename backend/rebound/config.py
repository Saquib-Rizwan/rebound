"""Runtime configuration. Everything is env-overridable; nothing secret is committed.

The whole project is designed to run with NO credentials at all. Keys upgrade the
demo from "reproducible offline" to "live", they are never required to reproduce
any number in the README.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    """Minimal .env reader so `python -m rebound` just works after a git clone.

    Deliberately does not overwrite variables already set in the real environment -
    CI and shell exports must win over a file someone forgot to delete.
    """
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()

DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
CACHE_DIR = DATA_DIR / "llm_cache"

# ---------------------------------------------------------------- LLM provider
# "auto" picks the first provider with credentials, else the offline fallback.
LLM_PROVIDER = os.getenv("REBOUND_LLM_PROVIDER", "auto")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("REBOUND_GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_API_VERSION = os.getenv("REBOUND_GEMINI_API_VERSION", "v1beta")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("REBOUND_ANTHROPIC_MODEL", "claude-opus-5")

LLM_TIMEOUT_S = float(os.getenv("REBOUND_LLM_TIMEOUT", "20"))
LLM_MAX_RETRIES = int(os.getenv("REBOUND_LLM_RETRIES", "3"))
LLM_WORKERS = int(os.getenv("REBOUND_LLM_WORKERS", "4"))
# Client-side ceiling. Free tiers publish a requests-per-minute limit and answer
# every request past it with a 429, so we pace ourselves rather than discovering
# the limit by being refused.
LLM_RPM = int(os.getenv("REBOUND_LLM_RPM", "20"))
LLM_CACHE_ENABLED = os.getenv("REBOUND_LLM_CACHE", "1") == "1"

# Published per-1M-token prices, used only for cost accounting in reports.
PRICES_USD_PER_MTOK = {
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini-3.1-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-flash-latest": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# ------------------------------------------------------------------- Razorpay
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
# Set this to the same string you type into the Razorpay dashboard webhook form.
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
# Hard default. Live execution must be switched on deliberately, never by accident.
DRY_RUN = os.getenv("REBOUND_DRY_RUN", "1") == "1"

# --------------------------------------------------------------- Agent limits
# These are the stopping rules. They are configuration, not magic numbers buried
# in the policy code, so a merchant can audit them without reading Python.
MAX_ATTEMPTS_PER_PAYMENT = int(os.getenv("REBOUND_MAX_ATTEMPTS", "3"))
CONTACT_COOLDOWN_HOURS = float(os.getenv("REBOUND_CONTACT_COOLDOWN_H", "24"))
MAX_CONTACTS_PER_CUSTOMER_PER_WEEK = int(os.getenv("REBOUND_MAX_CONTACTS_WEEK", "3"))
DAILY_ACTION_BUDGET_PAISE = int(os.getenv("REBOUND_DAILY_BUDGET_PAISE", "150000"))
QUIET_HOURS_START = int(os.getenv("REBOUND_QUIET_START", "21"))   # 21:00 IST
QUIET_HOURS_END = int(os.getenv("REBOUND_QUIET_END", "9"))        # 09:00 IST
MIN_TICKET_TO_CONTACT_PAISE = int(os.getenv("REBOUND_MIN_TICKET_PAISE", "5000"))

# ------------------------------------- RBI Digital Payments E-Mandate Framework
# Recurring debits in India are regulated, and the rules bind exactly what a
# recovery agent wants to do. These are compliance limits, not tuning knobs.
#
#  * a pre-debit notification must reach the customer at least 24 hours before
#    the debit, carrying merchant, amount, date, mandate reference and reason
#  * the first transaction on a mandate requires additional factor authentication;
#    later ones may skip AFA only below a threshold
#  * above the threshold, AFA is required again - so the debit cannot be a silent
#    retry, it has to go back through an authenticated flow
#
# Sources: RBI Digital Payments E-Mandate Framework (2026 consolidated directions).
PRE_DEBIT_NOTICE_HOURS = float(os.getenv("REBOUND_PRE_DEBIT_NOTICE_H", "24"))
AFA_THRESHOLD_PAISE = int(os.getenv("REBOUND_AFA_THRESHOLD_PAISE", "1500000"))       # INR 15,000
# Insurance premiums, mutual fund subscriptions and credit card bills carry a
# higher ceiling under the same framework.
AFA_THRESHOLD_HIGH_PAISE = int(os.getenv("REBOUND_AFA_THRESHOLD_HIGH_PAISE", "10000000"))  # INR 1,00,000
AFA_EXEMPT_MERCHANTS = set(
    filter(None, os.getenv("REBOUND_AFA_EXEMPT_MERCHANTS", "").split(","))
)

# Unit economics for the expected-value model (paise).
COST_PER_RETRY_PAISE = float(os.getenv("REBOUND_COST_RETRY", "200"))
COST_PER_WHATSAPP_PAISE = float(os.getenv("REBOUND_COST_WHATSAPP", "88"))
COST_PER_SMS_PAISE = float(os.getenv("REBOUND_COST_SMS", "15"))
COST_PER_EMAIL_PAISE = float(os.getenv("REBOUND_COST_EMAIL", "2"))
COST_PER_HUMAN_REVIEW_PAISE = float(os.getenv("REBOUND_COST_HUMAN", "4000"))
# Goodwill cost of one unwanted message. Not a real invoice - it is the price we
# put on annoying a customer, and it is why the agent stays quiet so often.
ANNOYANCE_PAISE = float(os.getenv("REBOUND_ANNOYANCE", "1200"))
MERCHANT_MARGIN = float(os.getenv("REBOUND_MARGIN", "0.35"))

POLICY_VERSION = "rebound-policy-v1.3"
