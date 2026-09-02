"""Customer-facing copy. Templated, never generated free-form.

A hard rule: **no untrusted text ever reaches a customer.** The gateway's
``error_description`` is attacker-influenced input (see `diagnose/sanitize.py`), so
it is used as evidence for classification and then dropped. What the customer
receives is a fixed template chosen by the *classified* root cause, with only our
own values interpolated - merchant name, amount, link.

This is also why an LLM does not write these. The model's job ends at picking a
label; letting it draft the outgoing message would put attacker-influenced text one
prompt away from a customer's phone, and would make every message unreviewable
before it went out.

Hinglish variants are included because that is how a large share of Indian
customers actually read transactional messages, and a stiff English template
converts worse. They are hand-written, not machine-translated.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from ..models import Decision
from ..taxonomy import FailureClass, InterventionType


@dataclass
class RenderedMessage:
    subject: str
    body: str
    language: str


# One line explaining the problem, one line telling them what to do. Nothing else.
TEMPLATES_EN: Dict[FailureClass, str] = {
    FailureClass.INSUFFICIENT_FUNDS:
        "Your payment of INR {amount} to {merchant} could not be completed as the "
        "bank reported insufficient balance. You can pay whenever you are ready: {link}",
    FailureClass.BANK_DOWNTIME:
        "Your payment of INR {amount} to {merchant} failed because your bank was "
        "temporarily unavailable. Nothing was deducted. Please try again: {link}",
    FailureClass.AUTH_DROPOFF:
        "You were one step away from completing your INR {amount} payment to "
        "{merchant}. Pick up where you left off: {link}",
    FailureClass.EXPIRED_INSTRUMENT:
        "Your saved card has expired, so your INR {amount} payment to {merchant} "
        "did not go through. Pay with another method: {link}",
    FailureClass.INVALID_INSTRUMENT:
        "We could not reach the account used for your INR {amount} payment to "
        "{merchant}. Try a different payment method: {link}",
    FailureClass.LIMIT_EXCEEDED:
        "Your bank declined the INR {amount} payment to {merchant} because it "
        "crossed a transaction limit. Another method should work: {link}",
    FailureClass.RISK_DECLINE_ISSUER:
        "Your bank declined the INR {amount} payment to {merchant}. This is a "
        "decision made by your bank, not by us. You can try another method: {link}",
    FailureClass.MANDATE_INACTIVE:
        "The auto-pay permission for your {merchant} subscription is no longer "
        "active, so the INR {amount} renewal failed. Re-authorise here: {link}",
    FailureClass.TECHNICAL_ERROR:
        "A technical issue on our side stopped your INR {amount} payment to "
        "{merchant}. Nothing was deducted. Please try again: {link}",
    FailureClass.CUSTOMER_CANCELLED:
        "Your INR {amount} payment to {merchant} was not completed. If that was a "
        "mistake, here is the link again: {link}",
}

TEMPLATES_HI: Dict[FailureClass, str] = {
    FailureClass.INSUFFICIENT_FUNDS:
        "{merchant} ko aapka INR {amount} ka payment complete nahi ho paaya - bank "
        "ne balance kam bataya. Jab convenient ho, yahan pay kar dijiye: {link}",
    FailureClass.BANK_DOWNTIME:
        "Aapka INR {amount} ka payment {merchant} ko fail ho gaya kyunki bank us "
        "waqt down tha. Paisa nahi kata hai. Dobara try kijiye: {link}",
    FailureClass.AUTH_DROPOFF:
        "Aap {merchant} ko INR {amount} ka payment karte karte ruk gaye the. Wahin "
        "se continue kijiye: {link}",
    FailureClass.EXPIRED_INSTRUMENT:
        "Aapka saved card expire ho chuka hai, isliye {merchant} ka INR {amount} "
        "payment nahi hua. Dusre method se pay kijiye: {link}",
    FailureClass.MANDATE_INACTIVE:
        "{merchant} subscription ki auto-pay permission active nahi hai, isliye INR "
        "{amount} ka renewal fail ho gaya. Yahan se dobara allow kijiye: {link}",
    FailureClass.TECHNICAL_ERROR:
        "Technical dikkat ki wajah se {merchant} ko aapka INR {amount} ka payment "
        "nahi ho paaya. Paisa nahi kata. Dobara try kijiye: {link}",
}

SUBJECTS: Dict[InterventionType, str] = {
    InterventionType.NUDGE_LINK: "Complete your payment to {merchant}",
    InterventionType.SWITCH_RAIL: "Try another way to pay {merchant}",
    InterventionType.REQUEST_REMANDATE: "Re-authorise your {merchant} subscription",
}

MERCHANT_NAMES = {
    "mrch_edtech": "Nova Learning",
    "mrch_d2c_apparel": "Kora Apparel",
    "mrch_saas": "Stackline",
    "mrch_grocery": "DailyCart",
}


def render_message(decision: Decision, language: str = "en", link: str = "{link}") -> RenderedMessage:
    """Builds the outgoing message from the *classified* cause only."""
    failure_class = decision.diagnosis.failure_class
    merchant = MERCHANT_NAMES.get(decision.merchant_id, "the merchant")
    amount = "{:,.0f}".format(decision.amount_paise / 100)

    templates = TEMPLATES_HI if language == "hi" else TEMPLATES_EN
    template = templates.get(failure_class) or TEMPLATES_EN.get(
        failure_class,
        "Your INR {amount} payment to {merchant} did not complete. You can pay here: {link}",
    )

    subject = SUBJECTS.get(
        decision.chosen.intervention, "About your payment to {merchant}"
    ).format(merchant=merchant)

    return RenderedMessage(
        subject=subject,
        body=template.format(amount=amount, merchant=merchant, link=link),
        language=language,
    )
