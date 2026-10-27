"""`extractor_version` naming (P2-C8). A pin, not a writer."""

from __future__ import annotations

__all__ = [
    "EXTRACTOR_VERSION_PREFIXES",
    "GOLD_EXTRACTOR_PREFIX",
    "HUMAN_EXTRACTOR_PREFIX",
    "WEB_EXTRACTOR_PREFIX",
]


HUMAN_EXTRACTOR_PREFIX = "human:"
WEB_EXTRACTOR_PREFIX = "web:"
GOLD_EXTRACTOR_PREFIX = "gold:"

EXTRACTOR_VERSION_PREFIXES: tuple[str, str, str, str] = (
    "<pipeline>@<semver-or-hash>",
    f"{HUMAN_EXTRACTOR_PREFIX}<oidc-sub>",
    f"{WEB_EXTRACTOR_PREFIX}<provider>@<version>",
    f"{GOLD_EXTRACTOR_PREFIX}<annotator>",
)
"""The four legal shapes of `FieldClaim.extractor_version`.

Machine pipelines use the first; a human resolution claim uses `human:` (D-16);
Story 3's fetched pages use `web:`; gold labels use `gold:` and are never
committed to a claim store (D-24). Track 0 does not write a claim.
"""
