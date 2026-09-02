"""Treat gateway error text as hostile input, because it is.

``error_description`` is free text that originated outside our trust boundary -
issuer switches, PSPs, and in some flows merchant-controlled metadata. The moment
that string is pasted into an LLM prompt it becomes an injection surface. This
module is the first of three layers that stop that:

  layer 1 (here)          strip and flag known injection markers, cap length
  layer 2 (llm.py)        the untrusted span is fenced and labelled as data
  layer 3 (llm.py)        output is a closed enum - the model cannot emit an action

Layer 3 is the one that actually holds. Layers 1 and 2 reduce the noise so that a
successful jailbreak still cannot express anything dangerous.
"""
from __future__ import annotations

import re
from typing import List, Tuple

MAX_LEN = 400

# Patterns that have no business appearing in a bank decline message.
INJECTION_PATTERNS: List[Tuple[str, str]] = [
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", "override_attempt"),
    (r"disregard\s+(the\s+)?(above|previous|system)", "override_attempt"),
    (r"\bsystem\s*prompt\b", "prompt_probe"),
    (r"you\s+are\s+now\s+", "role_reassign"),
    (r"\bnew\s+instructions?\b", "override_attempt"),
    (r"</?(system|instructions?|prompt)>", "tag_injection"),
    (r"\bretry\b[^.]{0,40}\b(\d{2,}|unlimited|forever)\s*times?", "action_injection"),
    (r"\b(classify|respond|answer|output)\s+(this\s+)?as\b", "label_steering"),
    (r"\bapprove\b|\bwhitelist\b|\bbypass\b", "control_steering"),
    (r"\{\{.*?\}\}|\$\{.*?\}", "template_injection"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE | re.DOTALL), tag) for p, tag in INJECTION_PATTERNS]
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f‪-‮⁦-⁩]")


def sanitize_untrusted(text: str) -> Tuple[str, List[str]]:
    """Returns (cleaned_text, flags). Flags are recorded on the Diagnosis for audit.

    We redact rather than reject: a decline we refuse to read is a decline we
    silently fail to recover, so the pipeline must keep working on hostile input.
    """
    if not text:
        return "", []

    flags: List[str] = []
    cleaned = _CONTROL.sub("", text)

    if len(cleaned) > MAX_LEN:
        cleaned = cleaned[:MAX_LEN]
        flags.append("truncated")

    for pattern, tag in _COMPILED:
        if pattern.search(cleaned):
            cleaned = pattern.sub("[redacted]", cleaned)
            if tag not in flags:
                flags.append(tag)

    # Fence-breaking: the prompt delimits untrusted text with these markers, so a
    # description containing them could close the fence early and escape.
    for marker in ("<<<UNTRUSTED", "UNTRUSTED>>>", "```"):
        if marker in cleaned:
            cleaned = cleaned.replace(marker, "[redacted]")
            if "fence_break" not in flags:
                flags.append("fence_break")

    return cleaned.strip(), flags


def is_hostile(flags: List[str]) -> bool:
    """True if the text tried to steer us, as opposed to merely being long."""
    return any(f != "truncated" for f in flags)
