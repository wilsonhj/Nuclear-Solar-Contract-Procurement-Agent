-- audit.run_event -- the run-scoped audit chain (contract C4, P2-C7).
--
-- clarifications.md D-13, "Where the run-scoped events live -- ADOPTED
-- 2026-08-07", and phase-2/story-4-persistence-runner.md section A.4.
--
-- WHY A SECOND TABLE. Two NFR-02 events have no document to chain to, and
-- `audit.event` is built so that it cannot hold an event without one:
--
--   * a gap-triggered web search (FR-WEB-01) happens *because* no document
--     supplied the value -- A-49;
--   * the compose-gate `--accept-incomplete` override (plan Decision 2,
--     FR-HITL-06) is a decision about a run, not about any one document.
--
-- `audit.event` makes "never global" structural with
-- `CHECK (stream = 'doc:' || document_id)` and a NOT NULL foreign key to
-- `public.document`. D-13 rejects folding run events in under a synthetic
-- stream because that "spends the structural stream CHECK and the document FK
-- -- the DDL's two strongest guarantees -- to avoid one CREATE TABLE". This is
-- the CREATE TABLE.
--
-- WHY CHAINED. NFR-02's letter ("cannot be altered or deleted after write") is
-- met by privilege separation alone. D-13 chains this table anyway because the
-- override is "the highest-repudiation-risk record in the system: the name of
-- the human who shipped a workbook past unresolved conflicts". Unchained, it
-- would be the one human decision without the tamper-evidence every resolution
-- gets.
--
-- SAME ENVELOPE, BYTE FOR BYTE. The preimage is D-13 section 2's object with
-- `"v": 1`, and the stream string is the only thing that differs from a
-- document event: `run:<run_id>` instead of `doc:<document_id>`. That is why
-- `audit.envelope.build_preimage` and `digest_of`, and `audit.writer.lock_stream`,
-- serve both tables unchanged. The prefixes are disjoint, so no stream can name
-- a chain in both tables, and the advisory-lock key is a function of the
-- stream string, so a run chain and a document chain never share a lock except
-- by 32-bit hash collision -- which costs serialisation, never correctness.
--
-- CALLER SEQUENCE: identical to sql/07's, via `audit.append_run_event`:
--
--   1. SELECT pg_advisory_xact_lock(hashtext(stream)::bigint);   -- own statement
--   2. SELECT seq, hash FROM audit.run_event
--        WHERE stream = $1 ORDER BY seq DESC LIMIT 1;             -- under the lock
--   3. build the envelope and digest in Python (D-13)
--   4. INSERT INTO audit.run_event (...) VALUES (...);           -- same txn
--
-- ⚠️ CONFIDENTIALITY IS AN OBLIGATION ON THE EMITTER HERE, NOT A PROPERTY OF
-- THIS TABLE. `audit.event` derives row visibility from its document's
-- `access_restricted` flag under FORCE ROW LEVEL SECURITY. A run event has no
-- document to derive it from, so this table carries no RLS and every role
-- granted SELECT below reads every row. That is right for what D-13 routes
-- here -- a web query concerns public-web information about a supplier and
-- model, and an override names a human and a run -- and it stops being right
-- the moment a payload quotes document content. **Emitters must reference
-- documents and conflicts by id only, never by value.** Nothing in this file
-- enforces that; it is stated here so that the first emitter (story 3's query
-- log, story 4b's `compose_override`) is written against it, and it is
-- registered in specs/001-procurement-agent/analysis.md as an open decision
-- rather than presented as solved.
--
-- Depends on: 00_roles.sql, 01_extensions_and_settings.sql (schema audit),
--             07_audit_event.sql (the tripwire functions, generalised below).

CREATE TABLE audit.run_event (
    event_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- The chain identity, hashed into every digest (D-13 section 2).
    stream             text NOT NULL,

    -- The run this chain belongs to. Opaque to this table: the runner (story
    -- 4b) mints it. Non-empty, because `run:` alone would be a stream naming
    -- no run, and a verifier asked for it by name would report "NOT FOUND"
    -- for a chain that the writer could in fact have produced.
    run_id             text NOT NULL CHECK (run_id <> ''),

    seq                bigint NOT NULL CHECK (seq >= 0),
    prev_hash          bytea CHECK (prev_hash IS NULL OR octet_length(prev_hash) = 32),
    hash               bytea NOT NULL CHECK (octet_length(hash) = 32),

    -- D-13 section 5 governs this exactly as it governs audit.event's CHECK:
    -- additions by amendment, never a removal or rename once a row exists.
    -- `audit.envelope.RUN_EVENT_TYPES_V1` must equal this list;
    -- tests/test_audit_envelope.py holds the two together.
    event_type         text NOT NULL CHECK (event_type IN (
                           'web_search',
                           'compose_override',
                           'run_started',
                           'run_finished'
                       )),
    actor              text NOT NULL,
    payload_canonical  text NOT NULL,
    payload            jsonb GENERATED ALWAYS AS (payload_canonical::jsonb) STORED NOT NULL,

    -- No DEFAULT, from creation. D-13 section 4: the caller supplies this and
    -- it is hashed; a defaulted timestamp cannot be pre-computed by the caller
    -- and so can never verify. A forgotten caller fails here on NOT NULL.
    recorded_at        timestamptz NOT NULL,

    CONSTRAINT audit_run_event_stream_matches_run
        CHECK (stream = 'run:' || run_id),
    CONSTRAINT audit_run_event_genesis_seq_zero
        CHECK ((prev_hash IS NULL) = (seq = 0)),

    -- Not in sql/07 as written, and added to audit.event by sql/12. A row whose
    -- prev_hash equals its own hash satisfies the self-referencing FK below *by
    -- itself* -- the parent it names is the row being inserted -- so without
    -- this CHECK every constraint passes. Planted at a high `seq`, it is what
    -- step 2's `ORDER BY seq DESC LIMIT 1` returns, and every honest append
    -- afterwards chains off it. No foreign key can express "not yourself".
    CONSTRAINT audit_run_event_no_self_parent
        CHECK (prev_hash IS DISTINCT FROM hash),

    -- Fork detection, loud rather than silent. NULLS NOT DISTINCT so that two
    -- genesis rows for one run (both prev_hash NULL) collide too.
    CONSTRAINT audit_run_event_no_fork UNIQUE NULLS NOT DISTINCT (stream, prev_hash),
    CONSTRAINT audit_run_event_seq_unique UNIQUE (stream, seq),
    -- A digest identifies at most one event per run, which is what lets the
    -- self-reference below be a foreign key at all.
    CONSTRAINT audit_run_event_hash_unique UNIQUE (stream, hash)
);

-- The chain has to be walkable, not merely fork-free: every non-genesis
-- event's parent must exist, in the same run's chain. Same shape and same
-- reasoning as audit_event_parent_exists in sql/07.
ALTER TABLE audit.run_event
    ADD CONSTRAINT audit_run_event_parent_exists
    FOREIGN KEY (stream, prev_hash)
    REFERENCES audit.run_event (stream, hash)
    ON DELETE RESTRICT
    ON UPDATE RESTRICT;

COMMENT ON TABLE audit.run_event IS
    'C4 run-scoped audit chain (D-13, P2-C7): web searches, compose overrides, '
    'run start and finish. Same envelope and preimage as audit.event; stream '
    'run:<run_id>. Immutable by GRANT; the triggers are a secondary tripwire. '
    'No RLS: payloads must reference documents by id, never by value.';

CREATE INDEX audit_run_event_type_idx ON audit.run_event (event_type);

-- Privilege separation is the boundary (plan.md Decision 9), exactly as for
-- audit.event: owned by the NOLOGIN audit_owner, INSERT and SELECT only to the
-- two LOGIN roles, nothing to PUBLIC. Granted explicitly rather than left to
-- sql/07's ALTER DEFAULT PRIVILEGES, which fires only for tables *created by*
-- audit_owner -- this file runs as the migration role and transfers ownership
-- afterwards, so a default privilege would never apply.
ALTER TABLE audit.run_event OWNER TO audit_owner;
REVOKE ALL ON audit.run_event FROM PUBLIC;
GRANT SELECT, INSERT ON audit.run_event TO procurement_app;
GRANT SELECT, INSERT ON audit.run_event TO procurement_ingest;

-- The tripwire functions sql/07 created named `audit.event` in their message
-- text, so attaching them to this table would make a rejected mutation here
-- report the wrong table. Replaced in place to name whichever table fired --
-- forward-only, and audit.event's message reads exactly as before.
CREATE OR REPLACE FUNCTION audit.reject_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
    AS $$
BEGIN
    RAISE EXCEPTION
        '%.% is append-only: % is rejected by trigger (secondary '
        'tripwire only; privilege separation is the actual boundary -- plan.md '
        'Decision 9). ALTER TABLE ... DISABLE TRIGGER and '
        'session_replication_role=replica both bypass this trigger with no DDL '
        'trace at all.', TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP;
END;
$$;
ALTER FUNCTION audit.reject_mutation() OWNER TO audit_owner;

CREATE OR REPLACE FUNCTION audit.reject_truncate() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
    AS $$
BEGIN
    RAISE EXCEPTION
        '%.% is append-only: TRUNCATE is rejected by trigger '
        '(secondary tripwire only, NOT the boundary -- plan.md Decision 9). '
        'This trigger is bypassed by ALTER TABLE ... DISABLE TRIGGER and by '
        'session_replication_role=replica, the latter leaving no DDL trace at '
        'all. It also cannot help against a superuser connection. The only '
        'durable answer to that threat model is shipping this log out '
        '(logical replication to a write-once sink), not another trigger.',
        TG_TABLE_SCHEMA, TG_TABLE_NAME;
END;
$$;
ALTER FUNCTION audit.reject_truncate() OWNER TO audit_owner;

CREATE TRIGGER audit_run_event_no_mutation
    BEFORE UPDATE OR DELETE ON audit.run_event
    FOR EACH ROW
    EXECUTE FUNCTION audit.reject_mutation();

CREATE TRIGGER audit_run_event_no_truncate
    BEFORE TRUNCATE ON audit.run_event
    FOR EACH STATEMENT
    EXECUTE FUNCTION audit.reject_truncate();
