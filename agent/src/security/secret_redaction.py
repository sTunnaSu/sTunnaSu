"""Conservative text redaction for errors crossing Phase 8 audit boundaries."""

from __future__ import annotations

import re


_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|access[_-]?token|refresh[_-]?token|"
        r"authorization|password)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ),
    re.compile(r"\bPK[A-Z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
)


def redact_sensitive_text(value: object) -> str:
    """Redact common credential forms without logging the matched value."""
    text = str(value)
    for pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text
