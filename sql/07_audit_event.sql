-- audit.event -- contract C4. The append-only, hash-chained audit envelope
-- (plan.md Decision 9; tasks.md WP-H).
--
-- Every extraction, conflict detection and resolution this pipeline performs is
-- expected to also write one of these rows, in the SAME transaction as the
-- business write. Rollback erasing the audit record is correct, not a bug: if
-- the extraction rolled back, it did not happen, and logging that it did would
-- be a false record. The genuine "we attempted X and it failed" case is a
-- different event class (event_type = attempt_failed below), logged from the
-- exception handler in a NEW transaction after the failure. Do not reach for
-- dblink or any other autonomous-transaction trick to make an audit row survive
-- the rollback of its own business transaction -- plan.md Decision 9 explicitly
-- rejects this, because it reintroduces exactly the failure (a logged event
-- that did not really happen) the same-transaction rule exists to prevent.
--
-- THE ADVISORY LOCK IS NOT IN THIS FILE, and that is deliberate. Decision 9's
-- measured finding: an advisory lock taken inside a trigger on this table does
-- not work, because the statement snapshot a trigger's queries run against is
-- taken before the trigger body acquires the lock -- a concurrent waiter still
-- reads a stale chain tip, and 8 concurrent writers produced 42 silent forks
-- under exactly this design. The fix that measured zero forks is a lock taken
-- as ITS OWN STATEMENT by the Python caller, before it reads the chain tip and
-- before it INSERTs. There is no way to express "take this lock first" as a
-- table constraint or a trigger without reintroducing the bug being fixed, so
-- it is not attempted here. The expected caller sequence (WP-H H.4), enforced
-- by code review and by the Python client library, NOT by this schema:
--
--   1. SELECT pg_advisory_xact_lock(hashtext(stream)::bigint);  -- own statement
--   2. SELECT hash FROM audit.event
--        WHERE stream = $1 ORDER BY seq DESC LIMIT 1;           -- read the tip
--   3. -- in Python, per WP-H H.2 and clarifications.md D-13 (ADOPTED
--      -- 2026-08-07), which settles the bytes this file previously left as
--      -- "...". Canonicalise with RFC 8785, NOT jsonb -- jsonb normalises key
--      -- order but preserves whatever numeric literal text it was given, so
--      -- 1.0 and 1.00 stay textually distinct.
--      --
--      -- The preimage is ONE JCS OBJECT, never a concatenation. An earlier
--      -- version of this comment sketched
--      --     hash := sha256(prev_hash || canonical_payload || ...)
--      -- and that is wrong twice: the trailing "..." left the field set
--      -- unenumerated, so two implementations could disagree about what is
--      -- covered while both believing they followed this comment; and a
--      -- delimiter-free concatenation is ambiguous by construction, since
--      -- distinct field values can produce identical bytes. D-13 section 2:
--      --
--      --     hash = SHA-256(JCS({
--      --         "v": 1, "stream", "seq", "event_type", "actor",
--      --         "recorded_at", "prev_hash": lowercase-hex or null,
--      --         "payload": {...}
--      --     }))
--      --
--      -- The object's keys ARE the enumeration, and JCS gives unambiguous
--      -- framing for free. Two details that change the hash and are easy to
--      -- read the other way: `payload` embeds as the PARSED JSON OBJECT, not
--      -- as the payload_canonical string; and `recorded_at` is supplied by
--      -- the caller as RFC 3339 UTC with the Z offset and fixed microsecond
--      -- precision, NOT defaulted -- a hashed timestamp is tamper-evident,
--      -- a defaulted one cannot be pre-computed by the caller.
--      --
--      -- "v": 1 is inside the preimage and is load-bearing. Without it,
--      -- changing the hashed field set later invalidates every existing
--      -- chain, because a verifier can no longer recompute historical
--      -- hashes. It must exist before the first event is ever emitted.
--   4. INSERT INTO audit.event (...) VALUES (...);              -- same txn
--
-- UNIQUE(stream, prev_hash) below turns any violation of that discipline into a
-- loud unique-violation error instead of a silent fork -- "necessary,
-- insufficient" per Decision 9: it catches a fork after the fact, it does not
-- prevent the race that causes one, which is why step 1 still matters.
--
-- Depends on: 01_extensions_and_settings.sql (schema audit), 02_document.sql.

CREATE TABLE audit.event (
    event_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Decision 9: "chain per document ... not globally, so cross-document
    -- concurrency stays unconstrained." The CHECK below makes "never global" a
    -- structural property of the table, not a convention a caller could
    -- forget: a stream literal like 'global' or an empty string fails at
    -- INSERT time, not at some later audit.
    stream             text NOT NULL,

    -- Redundant with `stream` by construction (see the CHECK below), kept as
    -- its own FK column rather than parsed out of `stream` at query time, so a
    -- stream can never name a document that does not exist.
    document_id        text NOT NULL REFERENCES public.document (document_id) ON DELETE RESTRICT,

    -- Ordering within a stream. A convenience for ORDER BY and for the H.5
    -- verification CLI, which would otherwise have to walk the linked list one
    -- prev_hash lookup at a time; it is NOT itself the concurrency-safety
    -- mechanism -- that is the advisory-lock discipline above, plus the UNIQUE
    -- constraints below.
    seq                bigint NOT NULL CHECK (seq >= 0),

    -- Hash chain. NULL prev_hash marks the one genesis event for a stream.
    -- Digest is SHA-256 (32 bytes), pinned by clarifications.md D-13 section 1.
    -- plan.md Decision 9 measures chain performance but never named an
    -- algorithm, so this file previously flagged it as its own assumption;
    -- D-13 closed that. The digest and these two CHECKs now move together or
    -- not at all.
    prev_hash          bytea CHECK (prev_hash IS NULL OR octet_length(prev_hash) = 32),
    hash               bytea NOT NULL CHECK (octet_length(hash) = 32),

    -- C4's event_type taxonomy. A CHECK, not a native enum type, for the same
    -- extensibility reason as document.document_type: a later ALTER TABLE ...
    -- DROP/ADD CONSTRAINT is far less awkward than ALTER TYPE ... ADD VALUE,
    -- which can never be dropped from once added. clarifications.md D-13
    -- (adopted 2026-08-07) makes these seven values **version 1**: additions are
    -- allowed by amendment to that decision, which is why a CHECK was the right
    -- choice here. Removing or renaming is forbidden once any event exists --
    -- that is what breaks a chain. Before the first emit there is no chain to
    -- break, which is how the unreachable 'web_search' value should go: it
    -- cannot be stored, because document_id is NOT NULL against a real document
    -- row and a gap-triggered search happens precisely because no document
    -- supplied the value (analysis.md A-49). D-13 routes those to a separately
    -- chained audit.run_event table. See sql/README.md decisions 5 and 6.
    event_type         text NOT NULL CHECK (event_type IN (
                           'document_ingested',
                           'parse_failure',
                           'extraction',
                           'web_search',
                           'conflict_detected',
                           'resolution',
                           'attempt_failed'
                       )),

    -- Who or what produced the event: a worker identity, a reviewer's email, a
    -- service name. Free text deliberately -- unlike event_type this is not a
    -- closed taxonomy the pipeline reasons over.
    actor              text NOT NULL,

    -- The RFC 8785 (JCS) canonical JSON text, produced in Python per WP-H H.2,
    -- NOT in SQL. `hash` above is computed over these exact bytes; this is
    -- what the H.5 verification CLI re-hashes to check the chain, never the
    -- `payload` column below.
    payload_canonical  text NOT NULL,

    -- Query and index convenience only, generated from payload_canonical so
    -- the two can never drift -- there is no application code path that
    -- writes one without the other. Never used to reverify the hash: casting
    -- back through jsonb is exactly the round-trip H.2's own reasoning warns
    -- against trusting as a canonicalisation step.
    payload            jsonb GENERATED ALWAYS AS (payload_canonical::jsonb) STORED NOT NULL,

    -- D-13 section 4: the caller supplies recorded_at (RFC 3339 UTC, Z offset,
    -- fixed microsecond precision) because the value is hashed. A DEFAULT would
    -- let a forgotten caller insert an unhashable timestamp that only surfaces
    -- at verification. NOT NULL with no default fails that caller at INSERT.
    recorded_at        timestamptz NOT NULL,

    -- A genesis event (no parent) must be seq 0, and every non-genesis event
    -- must have a parent; this ties the two redundant ordering mechanisms
    -- together so a caller bug that disagrees between them fails loudly at
    -- INSERT rather than producing a chain that looks fine until someone
    -- walks it.
    CONSTRAINT audit_event_stream_matches_document
        CHECK (stream = 'doc:' || document_id),
    CONSTRAINT audit_event_genesis_seq_zero
        CHECK ((prev_hash IS NULL) = (seq = 0)),

    -- Fork detection, loud rather than silent (Decision 9's own phrase).
    -- NULLS NOT DISTINCT (PostgreSQL 15+, available under Decision 3's
    -- PostgreSQL 18 pin) so that two genesis events for the same stream (both
    -- prev_hash NULL) collide too -- an ordinary UNIQUE constraint treats two
    -- NULLs as distinct and would let one stream grow two unrelated roots.
    CONSTRAINT audit_event_no_fork UNIQUE NULLS NOT DISTINCT (stream, prev_hash),
    CONSTRAINT audit_event_seq_unique UNIQUE (stream, seq),

    -- The chain has to be walkable, not merely fork-free.
    --
    -- `audit_event_no_fork` catches one shape of tampering: two children of the
    -- same parent. Review found three it does not, all accepted as
    -- procurement_app against the first version of this file:
    --
    --   1. a row whose prev_hash names a parent that never existed
    --   2. a second, disconnected root in the same stream (seq 900, prev_hash
    --      pointing at a fabricated digest) -- a fork made by starting a new
    --      segment rather than by branching an existing one, which is precisely
    --      what the comment above claims to prevent
    --   3. two rows in one stream sharing a `hash`, i.e. a chain loop
    --
    -- Each is silent, and each produces a chain that can never be verified.
    --
    -- Decision 9 leans on this chain as "the only mechanism that survives the
    -- superuser bypass -- a superuser can edit a row but cannot make the chain
    -- re-verify". **That claim is too broad, and this file no longer makes it.**
    -- src/procurement_agent/audit/verify.py, which is the module that has to be
    -- honest about it, states the guarantee that actually holds, and the wording
    -- below is deliberately the same so the two cannot drift:
    --
    --     An edit that is not re-sealed is detected.
    --
    -- That is worth having, and it is not tamper-proofing. The digest is unkeyed
    -- and the algorithm is public, so anyone who can edit a row can also
    -- recompute its digest; and because **nothing outside the chain pins the
    -- tip**, recomputing forward from the edited row to the end is always
    -- available. What that leaves undetectable is any *suffix rewritten in place
    -- and re-sealed*: measured against a live chain, a tail rewrite, a suffix
    -- rewrite and a full rewrite were all accepted (ok=True, 5 events), while
    -- the same edit left un-re-sealed was rejected at `seq 4: hash_mismatch`.
    -- A truncated tail is the special case where the rewritten suffix is empty,
    -- and it is not the cheapest one: re-attributing the last event costs one row
    -- edited and one digest recomputed -- no re-chaining, no DELETE, no TRUNCATE.
    -- So the GRANT above (no UPDATE, no DELETE, no TRUNCATE, to any role) and the
    -- tripwires at the foot of this file, which do cover truncation, do not cover
    -- this. The event it matters most for is the last `resolution` of every
    -- stream, which D-13 calls "the highest-repudiation-risk record in the
    -- system: the name of the human who shipped a workbook past unresolved
    -- conflicts" -- and which is by construction at the tip, where the rewrite is
    -- cheapest.
    --
    -- This is inherent to an unkeyed hash chain with no external witness, so it
    -- is a limit of the design rather than a defect to fix here. Decision 9's
    -- position stands as the mitigation -- privilege separation is the boundary
    -- and the chain is tamper-*evidence* layered on top -- and the durable answer
    -- is shipping this log to a write-once sink or otherwise witnessing the tip,
    -- which the tripwire comment below already names for truncation and which in
    -- fact covers the whole family.
    --
    -- None of which lets the three defects above off: a chain that was never
    -- walkable in the first place gives even the narrow guarantee nothing to
    -- stand on, because the un-re-sealed edit it detects is detected *by walking*.
    --
    -- UNIQUE (stream, hash) makes a digest identify at most one event per
    -- stream, which is what lets the self-reference below be a foreign key at
    -- all, and independently rules out (3).
    CONSTRAINT audit_event_hash_unique UNIQUE (stream, hash)
);

-- The self-reference: every non-genesis event's parent must exist, in the same
-- stream. Added after the table rather than inline because a self-referential
-- FK cannot be declared against a unique constraint defined in the same
-- CREATE TABLE statement.
--
-- NOT VALID is deliberately NOT used: this file creates the table empty, so
-- there is nothing to validate against and the constraint is enforced from the
-- first INSERT.
--
-- MATCH SIMPLE (the default) is what makes genesis work: with prev_hash NULL
-- the constraint is satisfied without a parent, so `audit_event_genesis_seq_zero`
-- above remains the thing that ties genesis to seq 0.
--
-- ON DELETE/UPDATE RESTRICT rather than CASCADE -- a cascade here would let one
-- deletion unravel a whole chain, which is the opposite of the property this
-- table exists to provide. The append-only triggers below already reject both
-- verbs; this is the same defence stated where a reader of the constraint will
-- see it.
ALTER TABLE audit.event
    ADD CONSTRAINT audit_event_parent_exists
    FOREIGN KEY (stream, prev_hash)
    REFERENCES audit.event (stream, hash)
    ON DELETE RESTRICT
    ON UPDATE RESTRICT;

COMMENT ON TABLE audit.event IS
    'C4 audit event envelope. Immutable: see the triggers below and the GRANT '
    'at the foot of this file. Privilege separation (00_roles.sql) is the '
    'actual boundary per plan.md Decision 9; the triggers are a free secondary '
    'tripwire that catches a mis-grant, nothing more.';

CREATE INDEX audit_event_document_id_idx ON audit.event (document_id);
CREATE INDEX audit_event_type_idx ON audit.event (event_type);

ALTER TABLE audit.event OWNER TO audit_owner;

REVOKE ALL ON audit.event FROM PUBLIC;
GRANT USAGE ON SCHEMA audit TO procurement_app;
GRANT SELECT, INSERT ON audit.event TO procurement_app;
GRANT USAGE ON SCHEMA audit TO procurement_ingest;
GRANT SELECT, INSERT ON audit.event TO procurement_ingest;

-- ---------------------------------------------------------------------------
-- Confidentiality (NFR-03, AC-8). `payload_canonical` -- and therefore the
-- generated `payload` -- is the extraction's own content: the price that was
-- read, the certification that was found. Measured as procurement_app against a
-- document that role could not see:
--
--     SELECT document_id FROM public.document WHERE document_id = 'doc-secret';
--      (0 rows)
--     SELECT document_id, event_type, payload FROM audit.event
--       WHERE document_id = 'doc-secret';
--      doc-secret | document_ingested | {"price_per_watt_dc": 0.19}
--      doc-secret | extraction        | {"price_per_watt_dc": 0.19}
--
-- An audit log that reproduces the material it audits is a copy of that
-- material, and NFR-03 does not carve out an exception for copies. The
-- derivation is the document this stream belongs to -- structurally exact here,
-- because `stream = 'doc:' || document_id` is a CHECK constraint on this table,
-- so an event's subject document is never ambiguous.
--
-- **This does not weaken Decision 9.** RLS governs SELECT visibility; it is not
-- and never was the immutability boundary -- that is the GRANT above (no UPDATE,
-- no DELETE, no TRUNCATE) plus the tripwires below. Nothing here can hide an
-- event from the H.5 verification CLI either: that runs as an operator identity
-- which sets `app.allow_restricted`, exactly as the confidentiality model
-- intends, and a chain walk that skipped rows would fail to verify rather than
-- pass quietly.
ALTER TABLE audit.event ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.event FORCE ROW LEVEL SECURITY;

CREATE POLICY audit_event_confidentiality_select ON audit.event
    FOR SELECT
    USING (
        NOT public.document_is_restricted(document_id)
        OR current_setting('app.allow_restricted', true) = 'true'
    );

-- INSERT needs its own policy or RLS's per-command default-deny starves the
-- GRANT above.
--
-- Note the consequence for the caller sequence at the top of this file: step 2
-- reads the chain tip with a SELECT, so a worker appending to a restricted
-- document's chain must connect as procurement_ingest (00_roles.sql), which the
-- policy below entitles. Under procurement_app that SELECT returns no rows and
-- the worker would compute a genesis event for a stream that already has one --
-- caught loudly by audit_event_no_fork, but caught late.
CREATE POLICY audit_event_write_insert ON audit.event
    FOR INSERT
    WITH CHECK (true);

-- procurement_app cannot switch its own confidentiality off. See the equivalent
-- policy in 02_document.sql for the measured attack matrix, why this is a new
-- RESTRICTIVE policy rather than an edit to the permissive ones, and why
-- `WITH CHECK (true)` is spelled out.
--
-- Keyed on public.document_is_restricted(document_id), matching
-- audit_event_confidentiality_select above. `document_id` is NOT NULL on this
-- table and `stream = 'doc:' || document_id` is a CHECK, so an event's subject
-- document is never ambiguous and the derivation is exact.
--
-- **This still does not weaken Decision 9.** The immutability boundary is the
-- GRANT (no UPDATE, no DELETE, no TRUNCATE) plus the tripwires below, none of
-- which this touches -- procurement_app is granted neither verb here, so the
-- `USING` side has no write to narrow. Nor can it hide an event from the H.5
-- verification CLI: that runs as an operator identity, which is not
-- procurement_app, so this policy does not apply to it at all. A chain walk that
-- silently skipped rows would fail to verify rather than pass quietly, which is
-- the property Decision 9 actually leans on.
CREATE POLICY audit_event_app_never_restricted ON audit.event
    AS RESTRICTIVE FOR ALL TO procurement_app
    USING (NOT public.document_is_restricted(document_id))
    WITH CHECK (true);

CREATE POLICY audit_event_ingest_select ON audit.event
    FOR SELECT TO procurement_ingest USING (true);

-- Permissive UPDATE/DELETE policies on the append-only table, for the reason
-- given at length in 04_claim.sql. Without them, enabling RLS above would have
-- quietly *disabled* the Decision 9 tripwire this file's whole second half is
-- about: RLS filters rows out before a FOR EACH ROW trigger runs, so
-- `UPDATE audit.event SET actor = 'x'` as audit_owner returned `UPDATE 0`
-- instead of raising. Decision 9's attack matrix lists that cell as
-- "Trigger: blocked", and a silent no-op is not blocked, it is unmeasured.
-- Neither verb is granted to any role; these policies only make a mis-grant
-- audible.
CREATE POLICY audit_event_tripwire_update ON audit.event FOR UPDATE USING (true);
CREATE POLICY audit_event_tripwire_delete ON audit.event FOR DELETE USING (true);

-- Forward-looking only: this affects audit tables that audit_owner itself
-- creates after this point (for example, via SET ROLE audit_owner in a later
-- migration), not audit.event above, which a bootstrap identity created and
-- then transferred to audit_owner via ALTER TABLE ... OWNER TO. A harmless
-- no-op until such a table exists.
ALTER DEFAULT PRIVILEGES FOR ROLE audit_owner IN SCHEMA audit
    GRANT SELECT, INSERT ON TABLES TO procurement_app;

-- ---------------------------------------------------------------------------
-- Secondary tripwires. Decision 9: "Add the trigger too -- it is free and
-- catches a mis-grant -- but do not count it as the boundary."
--
-- Both of the triggers below are bypassed by ALTER TABLE ... DISABLE TRIGGER,
-- and by session_replication_role = 'replica' -- the latter with NO DDL trace
-- at all, which is exactly the bypass Decision 9 says people forget. Both are
-- also bypassed outright by an actual PostgreSQL superuser, regardless of
-- FORCE ROW LEVEL SECURITY or any GRANT here. The only durable answer to that
-- threat model is shipping this log OUT -- logical replication to a
-- write-once sink -- which is out of scope for this DDL and is not attempted
-- here.
--
-- That answer is not specific to TRUNCATE, which is the narrow reading these
-- two triggers invite. Witnessing the tip externally is what covers the entire
-- undetectable family named on audit_event_hash_unique above -- any suffix
-- rewritten in place and re-sealed, of which a truncated tail is only the case
-- where the rewritten suffix is empty, and the most expensive one at that.
-- ---------------------------------------------------------------------------

CREATE FUNCTION audit.reject_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
    AS $$
BEGIN
    RAISE EXCEPTION
        'audit.event is append-only: % is rejected by trigger (secondary '
        'tripwire only; privilege separation is the actual boundary -- plan.md '
        'Decision 9). ALTER TABLE ... DISABLE TRIGGER and '
        'session_replication_role=replica both bypass this trigger with no DDL '
        'trace at all.', TG_OP;
END;
$$;

ALTER FUNCTION audit.reject_mutation() OWNER TO audit_owner;

CREATE TRIGGER audit_event_no_mutation
    BEFORE UPDATE OR DELETE ON audit.event
    FOR EACH ROW
    EXECUTE FUNCTION audit.reject_mutation();

-- BEFORE TRUNCATE must be its own trigger, declared FOR EACH STATEMENT: a
-- TRUNCATE trigger cannot be a row-level trigger, so it cannot be folded into
-- audit_event_no_mutation above even though the two share almost identical
-- wording.
CREATE FUNCTION audit.reject_truncate() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
    AS $$
BEGIN
    RAISE EXCEPTION
        'audit.event is append-only: TRUNCATE is rejected by trigger '
        '(secondary tripwire only, NOT the boundary -- plan.md Decision 9). '
        'This trigger is bypassed by ALTER TABLE ... DISABLE TRIGGER and by '
        'session_replication_role=replica, the latter leaving no DDL trace at '
        'all. It also cannot help against a superuser connection. The only '
        'durable answer to that threat model is shipping this log out '
        '(logical replication to a write-once sink), not another trigger.';
END;
$$;

ALTER FUNCTION audit.reject_truncate() OWNER TO audit_owner;

CREATE TRIGGER audit_event_no_truncate
    BEFORE TRUNCATE ON audit.event
    FOR EACH STATEMENT
    EXECUTE FUNCTION audit.reject_truncate();
