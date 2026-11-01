-- public.document -- contract C1.
--
-- One row per ingested source file. Matches SourceDocument in
-- src/procurement_agent/schema/component.py field-for-field. Identity is
-- content-addressed so a re-ingest of byte-identical input is a no-op, per
-- FR-ING-09 / NFR-05 / AC-5.
--
-- Depends on: 00_roles.sql.

CREATE TABLE public.document (
    document_id       text PRIMARY KEY,
    content_hash      text NOT NULL,
    source_uri        text NOT NULL,

    -- Mirrors DocumentType (schema/enums.py) verbatim, eight members. A CHECK,
    -- not a native enum type, so the taxonomy can later be extended with an
    -- ordinary ALTER TABLE ... DROP/ADD CONSTRAINT migration rather than
    -- ALTER TYPE ... ADD VALUE, which cannot be dropped from once added and,
    -- before PostgreSQL 12, could not even run inside a transaction with other
    -- DDL.
    document_type     text NOT NULL CHECK (document_type IN (
                           'contract_tos',
                           'purchase_order',
                           'environmental_regulation',
                           'terms_and_conditions',
                           'warranty',
                           'technical_documentation',
                           'pricing',
                           'spec_sheet'
                       )),

    ingested_at       timestamptz NOT NULL DEFAULT now(),
    data_vintage      timestamptz,

    -- NFR-03: contract and pricing documents are confidential. This is the
    -- coarse flag SourceDocument.access_restricted already fixes in the
    -- Pydantic schema; see the RLS policy below for how it is enforced at
    -- retrieval time, not only at the API edge.
    access_restricted boolean NOT NULL DEFAULT false,

    -- NFR-05 / AC-5, and the exact gap docs/agent-topology.md:34 names:
    -- "content_hash is the intended dedup key ... but it is an unconstrained
    -- field today ... a retry after a partial commit can still duplicate.
    -- Transactional hash uniqueness is a prerequisite for calling retries free,
    -- not a consequence of the field existing." This UNIQUE constraint is that
    -- prerequisite: the ingest worker's retry path becomes
    -- `INSERT ... ON CONFLICT (content_hash) DO NOTHING`, safe because the
    -- database refuses the duplicate rather than application logic merely
    -- being expected to check first.
    CONSTRAINT document_content_hash_unique UNIQUE (content_hash)
);

COMMENT ON TABLE public.document IS
    'C1 core table, matching SourceDocument (schema/component.py). See the GRANT '
    'statement below for exactly which columns the application may update.';

CREATE INDEX document_document_type_idx ON public.document (document_type);

ALTER TABLE public.document OWNER TO procurement_owner;

-- Decision 3c: FORCE so this policy applies even if a session ever connects as
-- the table owner by mistake. procurement_app is a non-owner already, so it was
-- always subject to the policy regardless of FORCE; FORCE closes the gap for
-- every other role, including procurement_owner itself, which is why the
-- policies below are deliberately not scoped `TO procurement_app` -- an
-- unscoped policy (default: PUBLIC) applies the same rule uniformly rather
-- than leaving the owner role staring at zero rows with no applicable policy
-- during a maintenance query. Only an actual PostgreSQL superuser bypasses
-- FORCE ROW LEVEL SECURITY, by design (plan.md Decision 9: "a superuser
-- bypasses even FORCE RLS").
ALTER TABLE public.document ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.document FORCE ROW LEVEL SECURITY;

-- The coarse confidentiality gate from NFR-03. Deliberately NOT the
-- fine-grained per-query `allowed_document_ids` allowlist that
-- VectorStorePort.search (ports/__init__.py) takes as a parameter -- that stays
-- a WHERE-clause concern inside the store adapter, because RLS is a poor fit
-- for a large, dynamic, per-request ID set (it would need a session GUC
-- re-issued on every call). RLS here is the boundary a caller cannot forget to
-- apply; allowed_document_ids is the boundary that scopes *which*
-- unrestricted-or-authorized documents one specific query may see. This
-- deliberately implements no more than the field the frozen Pydantic schema
-- already commits to; clarifications.md D-15 (adopted provisionally 2026-08-07)
-- has since ratified exactly that minimum as C7's answer, rather than replacing
-- it -- one document-level label, per-principal clearance, and
-- allowed_document_ids as scoping *within* an entitlement rather than the
-- boundary. It stays provisional pending two facts about NDA scope and
-- evaluator conflicts; either one turns the label into a group. See
-- sql/README.md's decisions list.
--
-- current_setting(..., true) returns NULL when the GUC was never set for this
-- session, and NULL is never true, so an application that forgets to set
-- app.allow_restricted fails closed (restricted documents hidden), not open.
CREATE POLICY document_confidentiality_select ON public.document
    FOR SELECT
    USING (
        NOT access_restricted
        OR current_setting('app.allow_restricted', true) = 'true'
    );

-- RLS's default-deny is per command, independently of any other policy on the
-- table: with FORCE enabled and only a SELECT policy defined, INSERT/UPDATE
-- would see zero eligible rows even though the GRANT below allows them. These
-- two policies keep the write path exactly where the GRANT puts it; only the
-- SELECT policy above is meant to restrict anything, per Decision 3c's scope
-- (RLS is for retrieval-time visibility; the immutability boundary is
-- privilege separation, Decision 9 -- not RLS).
CREATE POLICY document_write_insert ON public.document
    FOR INSERT
    WITH CHECK (true);

-- The UPDATE policy carries the confidentiality predicate for the reason set
-- out at length in 03_chunk.sql: `USING (true)` plus `access_restricted` inside
-- the granted column set let procurement_app declassify every restricted row
-- with one WHERE-less UPDATE, because an UPDATE needs no SELECT. An entitled
-- session -- one that has set `app.allow_restricted` -- can still correct a
-- misclassification, which is what the narrow grant below exists for.
CREATE POLICY document_write_update ON public.document
    FOR UPDATE
    USING (
        NOT access_restricted
        OR current_setting('app.allow_restricted', true) = 'true'
    )
    WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- **procurement_app cannot switch its own confidentiality off.**
--
-- Every policy above admits an entitled session with
-- `current_setting('app.allow_restricted', true) = 'true'`. Measured on a live
-- pgvector/pgvector:0.8.6-pg16 with 00-08 applied unchanged, procurement_app
-- sets that GUC itself, on all seven content tables at once:
--
--     as procurement_app:                            1 row visible per table
--     SET app.allow_restricted = 'true';             2 rows -- incl. the
--                                                    CONFIDENTIAL pricing chunk
--
-- and three further vectors reach the identical result -- `set_config(...)`,
-- `SET LOCAL` inside a transaction, and, worst,
--
--     psql "user=procurement_app dbname=procurement \
--           options='-c app.allow_restricted=true'" -c "SELECT ..."
--
-- which arrives **fully declassified with no statement issued at all**. So
-- anyone holding the application DSN had full disclosure, and an application-side
-- "RESET on pool checkout" guard cannot cover it: the value is already in place
-- before the pool's first statement runs. The GUC is an entitlement assertion
-- the application makes about its caller; it was never a boundary the
-- application is on the far side of, and until this policy existed nothing in
-- the schema said so.
--
-- **Why a new RESTRICTIVE policy and not an edit to the ones above.**
--
--   * Deleting the `OR current_setting(...)` clause breaks ingest. The chunk
--     inheritance trigger's parent lookup dies with
--     `chunk.document_id doc-secret has no document row`, because
--     public.document_is_restricted() reaches visibility through exactly that
--     clause. Repairing it needs a fresh `TO procurement_owner USING (true)`
--     policy -- a wider hole than the one being closed.
--   * Adding a stricter-looking *permissive* policy changes nothing. Permissive
--     policies are OR'd, so a new one joins the eleven already here with `OR`
--     and can only ever widen the result. This was measured the hard way.
--
--   `AS RESTRICTIVE` is AND'd with the OR of every permissive policy. One such
--   policy per table therefore closes the escape without touching a single
--   existing policy, without a new column, and without disturbing ingest.
--
-- **Scoped `TO procurement_app`, and that scoping is load-bearing.** The other
-- three roles must keep their entitled reads: procurement_ingest writes
-- restricted rows and reads them back through RETURNING (00_roles.sql), and both
-- owner roles evaluate these policies too under FORCE ROW LEVEL SECURITY --
-- including inside public.document_is_restricted(), which runs SECURITY DEFINER
-- as procurement_owner. Widening this policy to PUBLIC would make that helper's
-- own lookup return zero rows, whereupon its `coalesce(..., true)` reports every
-- document restricted and the entire store disappears for everyone. Fail-closed,
-- but total.
--
-- **A definer function is only a control if RLS still applies to its owner.**
-- The obvious alternative fix -- route reads through a SECURITY DEFINER
-- accessor and revoke the tables -- is not a control at all unless the function's
-- owner is itself subject to RLS. Measured here: the same accessor owned by
-- procurement_owner returns the filtered set (FORCE ROW LEVEL SECURITY applies
-- to owners), while owned by a superuser it silently returns **everything**,
-- restrictive policy and all, with no error and nothing in its text to warn a
-- reader. That is why public.document_is_restricted() and
-- public.conflict_is_restricted() are owned by procurement_owner and audit_owner
-- respectively rather than by the bootstrap identity that creates them, and why
-- 00_roles.sql asserts NOSUPERUSER/NOBYPASSRLS on every one of these four roles
-- on every apply.
--
-- **`WITH CHECK (true)` is spelled out, not defaulted.** PostgreSQL reuses the
-- `USING` expression as the `WITH CHECK` when the latter is omitted, which would
-- make `INSERT ... access_restricted = true` fail for procurement_app while the
-- same insert with `false` succeeded -- measured. That is precisely the
-- "the schema penalised the safe action" defect 00_roles.sql documents at length
-- and solved by creating procurement_ingest; re-creating it one table over would
-- be a regression, not a hardening. Restriction therefore stays a ratchet for
-- this role: `USING` denies it any restricted row to read, update or delete,
-- while `WITH CHECK (true)` leaves it free to *raise* restriction, never lower
-- it. The write path is unchanged; only the read/target side is closed.
CREATE POLICY document_app_never_restricted ON public.document
    AS RESTRICTIVE FOR ALL TO procurement_app
    USING (NOT access_restricted)
    WITH CHECK (true);

-- document is append-only in spirit (document_id is content-addressed
-- identity) but not literally immutable the way claim/resolution/audit.event
-- are: no reduction logic anywhere depends on a document row never changing,
-- and access_restricted or data_vintage may legitimately need correcting after
-- a misclassification at ingest. UPDATE is therefore granted narrowly to just
-- those two columns rather than withheld entirely or opened on the whole row;
-- this is a judgement call the specs do not make either way -- see
-- sql/README.md. There is no DELETE path: nothing in the spec ever removes an
-- ingested document, and chunk/claim/audit.event all reference it by
-- document_id.
GRANT SELECT, INSERT ON public.document TO procurement_app;
GRANT UPDATE (access_restricted, data_vintage) ON public.document TO procurement_app;

-- ---------------------------------------------------------------------------
-- The ingest principal (00_roles.sql, procurement_ingest).
--
-- These policies are scoped `TO procurement_ingest` -- the one place in this
-- schema where a policy IS role-scoped, against the general rule stated above.
-- The rule exists so an ops session as procurement_owner does not stare at zero
-- rows; here the scoping is the entire point, because the whole difference
-- between the write principal and the retrieval principal is which rows each may
-- see. An unscoped `USING (true)` would hand procurement_app the same
-- unrestricted read and delete the control.
--
-- Why this is needed at all: RLS applies a table's `FOR SELECT` policy as a
-- WITH CHECK against the proposed row whenever an INSERT carries `RETURNING` or
-- `ON CONFLICT`, so the documented idempotent-ingest idiom eleven lines above
-- --
--     INSERT ... VALUES (..., access_restricted => true)
--     ON CONFLICT (content_hash) DO NOTHING;
--
-- -- failed with "new row violates row-level security policy" for exactly the
-- confidential documents it exists to serve, while the same statement with
-- `access_restricted => false` succeeded. Full reasoning, and the two rejected
-- alternatives (an `xmin` read-back policy, which does not work; reusing
-- `app.allow_restricted`, which leaks through a connection pool), are in
-- 00_roles.sql above the CREATE ROLE.
--
-- The UPDATE policy is `USING (true)` for this role deliberately, where
-- procurement_app's carries the confidentiality predicate. That predicate exists
-- to stop a role *declassifying a row it cannot read*; procurement_ingest can
-- read every row, so there is no blind write to prevent -- the reclassification
-- path this table's narrow column grant was written for is precisely this
-- role's job. The column grant still bounds it to the same two columns.
CREATE POLICY document_ingest_select ON public.document
    FOR SELECT TO procurement_ingest USING (true);
CREATE POLICY document_ingest_insert ON public.document
    FOR INSERT TO procurement_ingest WITH CHECK (true);
CREATE POLICY document_ingest_update ON public.document
    FOR UPDATE TO procurement_ingest USING (true) WITH CHECK (true);

GRANT SELECT, INSERT ON public.document TO procurement_ingest;
GRANT UPDATE (access_restricted, data_vintage) ON public.document TO procurement_ingest;

-- ---------------------------------------------------------------------------
-- The shared confidentiality derivation, used by the RLS policies on every
-- downstream table that carries a document_id (claim, audit.event, job) and,
-- one hop further out, by conflict/resolution via 05_conflict.sql.
--
-- **Why this function exists.** RLS was originally applied only to `document`
-- and `chunk`, on the reasoning that those are the two tables on the retrieval
-- path. Review measured what the other tables then do, as procurement_app,
-- against a document that role cannot see:
--
--     SELECT document_id FROM public.document WHERE document_id = 'doc-secret';
--      (0 rows)
--     SELECT document_id, field, value, verbatim_value FROM public.claim
--       WHERE document_id = 'doc-secret';
--      doc-secret | price_per_watt_dc | 0.19 | CONFIDENTIAL 0.19 USD/W
--     SELECT payload FROM audit.event WHERE document_id = 'doc-secret';
--      {"price_per_watt_dc": 0.19}
--
-- `claim.value`/`verbatim_value`, `audit.event.payload`, `conflict.explanation`,
-- `resolution.value_before`/`value_after` and `job.payload` all carry document
-- content, not just references to it. A confidentiality control on `document`
-- and `chunk` alone does not implement NFR-03 or AC-8; it implements them on two
-- of the seven tables that hold the material.
--
-- **Derived, not denormalised.** `chunk` carries its own `access_restricted`
-- column filled by a BEFORE trigger, because a chunk may be *more* restricted
-- than its parent (a confidential paragraph in an open datasheet) and so needs
-- somewhere to record that. No such case exists for claim/audit.event/job: their
-- restriction is exactly their document's. Deriving it on read rather than
-- copying it therefore removes a whole class of bug rather than adding one --
-- reclassifying a document takes effect immediately on every dependent table,
-- with no backfill to forget and no column that can drift out of step.
--
-- SECURITY DEFINER with `SET app.allow_restricted`, for the reason spelled out
-- at length on chunk_inherit_access_restricted() in 03_chunk.sql: `document` is
-- FORCE ROW LEVEL SECURITY, which applies to procurement_owner too, so
-- ownership alone would make this lookup return zero rows and every dependent
-- policy fail closed on every restricted document. The setting is scoped to the
-- function invocation and reverts on exit, so it cannot leak to the caller. The
-- body reads one boolean from one table by primary key, takes no caller-supplied
-- SQL, and returns nothing else.
--
-- STABLE, not IMMUTABLE: the answer changes when a document is reclassified.
-- STABLE is what lets the planner call it once per document_id within a
-- statement instead of once per row.
CREATE FUNCTION public.document_is_restricted(p_document_id text)
    RETURNS boolean
    LANGUAGE sql
    STABLE
    AS $$
    -- NULL document_id (only job.document_id is nullable -- compose_workbook is
    -- a whole-store stage with no single subject) is not restricted.
    -- coalesce(..., true) is the fail-closed direction for the case a
    -- document_id names no row: every such column is a FK so it cannot happen,
    -- but if it ever does, hiding the row is the safe answer.
    SELECT CASE
        WHEN p_document_id IS NULL THEN false
        ELSE coalesce(
            (SELECT d.access_restricted FROM public.document d
              WHERE d.document_id = p_document_id),
            true)
    END;
$$ SECURITY DEFINER
   SET search_path = pg_catalog, pg_temp
   SET app.allow_restricted = 'true';

ALTER FUNCTION public.document_is_restricted(text) OWNER TO procurement_owner;

-- Not left on PUBLIC: the boolean is a small oracle ("is this document id
-- confidential"), so EXECUTE goes to exactly the roles whose policies call it.
-- Both owner roles are included because FORCE ROW LEVEL SECURITY means their
-- own maintenance queries evaluate these policies too.
REVOKE EXECUTE ON FUNCTION public.document_is_restricted(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.document_is_restricted(text)
    TO procurement_app, procurement_ingest, procurement_owner, audit_owner;
