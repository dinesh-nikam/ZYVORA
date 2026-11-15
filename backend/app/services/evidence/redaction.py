"""Evidence minimisation: keep only what is needed to justify a finding.

Third-party snippets can contain unrelated personal data.  Before evidence is
stored or reported we:

* mask e-mail addresses,
* mask every phone number that is *not* the investigated number,
* collapse whitespace and truncate to a short excerpt.
"""

from __future__ import annotations

import re

from app.services.phone.matching import NumberVariants

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONEISH = re.compile(r"(?<![\w])\+?\(?\d[\d\s().\-/]{6,}\d(?![\w])")
_WS = re.compile(r"\s+")

MAX_EXCERPT = 400


def redact_excerpt(text: str | None, variants: NumberVariants | None, *, limit: int = MAX_EXCERPT) -> str:
    if not text:
        return ""
    value = _WS.sub(" ", text).strip()
    value = _EMAIL.sub("[email redacted]", value)

    def _mask(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if variants is not None and digits in variants.digit_forms:
            return "[investigated number]"
        return "[other number redacted]"

    value = _PHONEISH.sub(_mask, value)
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value
