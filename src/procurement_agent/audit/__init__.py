"""WP-H - the audit envelope, contract C4.

`audit.event` is the append-only, hash-chained record of every extraction,
conflict detection and resolution the pipeline performs (NFR-02, plan Decision
9). The table shipped first; this is the library that computes what goes in it.
**Nothing may emit an event until this exists** - not because the bytes were
unknown, but because nothing could compute them.

The four pieces, each owning one of D-13's answers:

* `canonical` (H.2) - RFC 8785 canonicalisation, in Python and not in SQL.
* `envelope` (H.3) - the preimage, the SHA-256 digest, and the v1 taxonomy.
* `writer` (H.4) - the advisory lock, as its own statement before the INSERT.
* `verify` (H.5) - the chain walk and its CLI.

**Why C4 uses JCS while C6 uses sorted-key JSON with `repr` floats.** D-13 asks
for this boundary to be restated in WP-H rather than in the decision record, and
`envelope` carries the long version. In short: an audit chain must be verifiable
by an implementation that is not this one, so cross-language agreement outranks
Python fidelity, and ECMAScript number rules collapsing `650.0` to `650` is a
price C4 pays. The workbook projection cannot pay it, because that int/float
distinction is data the store genuinely carries.

**Two chains, one envelope (P2-C7).** Document-scoped events go to
`audit.event` through `append_event`; run-scoped events - gap-triggered web
searches (A-49), the `--accept-incomplete` compose-gate override, run start and
finish - go to `audit.run_event` (`sql/11`) through `append_run_event`. D-13
requires the second to reuse the first's envelope and lock discipline
"verbatim", and it does: both builders seal through one function, and the
stream string (`doc:` or `run:`) is the only input that differs. `sql/12`
removed `web_search` from `audit.event`'s taxonomy, which could never store it.
`recorded_at` has no default on either table: section 4's caller-supplied value
is required at INSERT. Updating `sql/README.md` decisions 5-7 remains
outstanding.
"""

from __future__ import annotations

from .canonical import (
    CanonicalisationError,
    JsonObject,
    JsonValue,
    canonical_text,
    canonicalise,
)
from .envelope import (
    ENVELOPE_VERSION,
    EVENT_TYPES_V1,
    KNOWN_ENVELOPE_VERSIONS,
    RUN_EVENT_TYPES_V1,
    RUN_STREAM_PREFIX,
    STREAM_PREFIX,
    AuditEvent,
    ChainTip,
    RunAuditEvent,
    UnknownEventTypeError,
    build_event,
    build_preimage,
    build_run_event,
    digest_of,
    document_id_for_stream,
    format_recorded_at,
    run_id_for_stream,
    stream_for_document,
    stream_for_run,
    taxonomy_for_stream,
)
from .verify import (
    ChainDefect,
    ChainReport,
    DefectKind,
    StoredEvent,
    read_events,
    read_streams,
    verify_events,
    verify_stream,
)
from .writer import (
    AuditConnection,
    append_event,
    append_run_event,
    insert_event,
    insert_run_event,
    lock_stream,
    read_chain_tip,
    read_run_chain_tip,
)

__all__ = [
    "ENVELOPE_VERSION",
    "EVENT_TYPES_V1",
    "KNOWN_ENVELOPE_VERSIONS",
    "RUN_EVENT_TYPES_V1",
    "RUN_STREAM_PREFIX",
    "STREAM_PREFIX",
    "AuditConnection",
    "AuditEvent",
    "CanonicalisationError",
    "ChainDefect",
    "ChainReport",
    "ChainTip",
    "DefectKind",
    "JsonObject",
    "JsonValue",
    "RunAuditEvent",
    "StoredEvent",
    "UnknownEventTypeError",
    "append_event",
    "append_run_event",
    "build_event",
    "build_preimage",
    "build_run_event",
    "canonical_text",
    "canonicalise",
    "digest_of",
    "document_id_for_stream",
    "format_recorded_at",
    "insert_event",
    "insert_run_event",
    "lock_stream",
    "read_chain_tip",
    "read_events",
    "read_run_chain_tip",
    "read_streams",
    "run_id_for_stream",
    "stream_for_document",
    "stream_for_run",
    "taxonomy_for_stream",
    "verify_events",
    "verify_stream",
]
