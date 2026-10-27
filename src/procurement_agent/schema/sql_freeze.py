"""SQL shapes Track 0 freezes; 4a/7 write the files (P2-C6 / P2-C7)."""

from __future__ import annotations

__all__ = [
    "CLAIM_HUMAN_RESOLUTION_CHECK",
    "CLAIM_HUMAN_RESOLUTION_CONSTRAINT",
    "RUN_EVENT_TYPES",
    "SQL_RESERVED_FILES",
]


CLAIM_HUMAN_RESOLUTION_CONSTRAINT = "claim_human_carries_resolution"
CLAIM_HUMAN_RESOLUTION_CHECK = "(extractor_version LIKE 'human:%') = (resolution_id IS NOT NULL)"
RUN_EVENT_TYPES: frozenset[str] = frozenset(
    {"web_search", "compose_override", "run_started", "run_finished"}
)
SQL_RESERVED_FILES: tuple[str, ...] = (
    "10_claim_resolution_link.sql",
    "11_audit_run_event.sql",
    "12_audit_event_taxonomy.sql",
    "13_access_denylist.sql",
    "14_restricted_group.sql",
)
