-- public.conflict / public.conflict_candidate -- contract C1, C5
-- (ConflictQueueEntry, schema/field.py).
--
-- One conflict row per queued disagreement awaiting a human decision. WP-F F.1:
-- lease-based claiming via `SELECT ... FOR UPDATE SKIP LOCKED` and a 15-minute
-- lease (D-12e).
--
-- Depends on: 04_claim.sql.

CREATE TABLE public.conflict (
    -- ConflictQueueEntry.entry_id is typed `str` in the frozen schema. This
    -- primary key matches that name and type exactly rather than introducing a
    -- second surrogate identity for the same concept.
    entry_id           text PRIMARY KEY,

    field_name         text NOT NULL,
    supplier           text NOT NULL,
    model              text NOT NULL,
    component_category text NOT NULL CHECK (component_category IN (
                           'pv_modules', 'inverters_pcs', 'trackers_mounting',
                           'transformers', 'cabling_wiring', 'combiner_boxes',
                           'bess', 'ems_scada'
                       )),
    conflict_class     text NOT NULL CHECK (conflict_class IN (
                           'record_vs_web', 'inter_document', 'intra_document',
                           'temporal', 'unit_normalization'
                       )),

    -- Severity (schema/enums.py) is an IntEnum where higher is worse (0
    -- INFORMATIONAL .. 4 CRITICAL), stated explicitly there because "a
    -- P1/P2/P3 reading would invert it." Repeating the direction here for the
    -- same reason -- a reviewer of this DDL should not have to cross-reference
    -- Python to know which end of the range is dangerous.
    severity           smallint NOT NULL CHECK (severity BETWEEN 0 AND 4),

    explanation        text NOT NULL,
    detected_at        timestamptz NOT NULL DEFAULT now(),

    -- Queue-item lifecycle -- distinct from CanonicalField.ConflictStatus,
    -- which is a per-field flag on the canonical projection, not on this queue
    -- row.
    status             text NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'leased', 'resolved')),
    lease_owner        text,
    lease_expires_at   timestamptz,

    -- FR-HITL-04's REQUEST_MORE_WEB_SEARCH reopens a conflict rather than
    -- closing it; WP-F F.3 caps that at 3, then forces a terminal decision. The
    -- CHECK is a backstop, not the enforcement -- the application still has to
    -- refuse a 4th reopen itself and explain why; this only guarantees the
    -- count cannot drift past the cap even if it forgets.
    reopen_count       integer NOT NULL DEFAULT 0 CHECK (reopen_count BETWEEN 0 AND 3),

    -- Defends against a half-set lease (owner without an expiry, or vice
    -- versa), which would be a bug in the claiming code, not a valid state.
    CONSTRAINT conflict_lease_fields_consistent
        CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))

    -- D-12c: "conflicts never expire ... they age, and age is a reported
    -- metric." Age is `now() - detected_at`; there is deliberately no TTL or
    -- expiry column, and no DELETE path anywhere in this file.
);

COMMENT ON TABLE public.conflict IS
    'C1 core table, matching ConflictQueueEntry (schema/field.py). '
    'Lease-claiming pattern (WP-F F.1): '
    'SELECT entry_id FROM conflict WHERE status = ''pending'' '
    'ORDER BY detected_at FOR UPDATE SKIP LOCKED LIMIT n; '
    'then UPDATE conflict SET status = ''leased'', lease_owner = $1, '
    'lease_expires_at = now() + interval ''15 minutes'' WHERE entry_id = ANY($2).';

CREATE INDEX conflict_status_idx ON public.conflict (status);
CREATE INDEX conflict_lease_expiry_idx ON public.conflict (lease_expires_at)
    WHERE status = 'leased';
CREATE INDEX conflict_field_supplier_model_idx ON public.conflict (field_name, supplier, model);

ALTER TABLE public.conflict OWNER TO procurement_owner;

-- conflict is a mutable queue row, unlike claim/resolution/audit.event, but
-- only the queue-management columns are ever mutable after detection: the
-- descriptive facts a conflict was detected with (field_name, supplier, model,
-- category, class, severity, explanation, detected_at) are set once at INSERT
-- and never revised. Column-level UPDATE enforces that split at the privilege
-- layer rather than relying only on application discipline.
GRANT SELECT, INSERT ON public.conflict TO procurement_app;
GRANT UPDATE (status, lease_owner, lease_expires_at, reopen_count) ON public.conflict TO procurement_app;

-- public.conflict_candidate -- normalises ConflictQueueEntry.candidates
-- (schema/field.py) into references onto claim, rather than a denormalised
-- copy of ConflictCandidate. Safe because claim rows are immutable: there is
-- no staleness risk a snapshot would have protected against, and a join
-- reconstructs the exact ConflictCandidate shape (value, unit, verbatim_value,
-- condition, source_tier, source_ref, confidence) at read time.
CREATE TABLE public.conflict_candidate (
    entry_id  text NOT NULL REFERENCES public.conflict (entry_id) ON DELETE CASCADE,
    claim_id  bigint NOT NULL REFERENCES public.claim (claim_id) ON DELETE RESTRICT,
    -- FR-HITL-03's candidate list has a presentation order; ordinal preserves
    -- it deterministically rather than depending on physical row order
    -- surviving a read.
    ordinal   integer NOT NULL,
    PRIMARY KEY (entry_id, claim_id),
    CONSTRAINT conflict_candidate_ordinal_unique UNIQUE (entry_id, ordinal)
);

COMMENT ON TABLE public.conflict_candidate IS
    'Junction table: which claim rows make up one conflict''s candidate list. '
    'See sql/README.md''s decisions list for why this is references, not a '
    'denormalised copy.';

CREATE INDEX conflict_candidate_claim_id_idx ON public.conflict_candidate (claim_id);

ALTER TABLE public.conflict_candidate OWNER TO procurement_owner;

GRANT SELECT, INSERT ON public.conflict_candidate TO procurement_app;
GRANT SELECT, INSERT ON public.conflict_candidate TO procurement_ingest;

ALTER TABLE public.conflict_candidate ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conflict_candidate FORCE ROW LEVEL SECURITY;

-- Visibility inherits the parent `conflict` row via EXISTS, not via
-- `conflict_is_restricted()`. That function reads this table: a policy here
-- that called it would recurse, which PostgreSQL rejects at query time.
--
-- `procurement_owner` / `audit_owner` get USING (true) so the SECURITY DEFINER
-- walk inside `conflict_is_restricted` still sees every candidate row under
-- FORCE RLS. Without that owner policy no policy applies to the owner, FORCE
-- RLS default-denies, the walk sees no candidates, and every conflict reads as
-- unrestricted - failing open. The EXISTS policy below cannot stand in: it is
-- scoped to procurement_app, and a PUBLIC version would apply inside the walk
-- and re-enter `conflict_is_restricted`.
CREATE POLICY conflict_candidate_owner ON public.conflict_candidate
    FOR ALL TO procurement_owner, audit_owner
    USING (true)
    WITH CHECK (true);

CREATE POLICY conflict_candidate_select ON public.conflict_candidate
    FOR SELECT TO procurement_app
    USING (
        EXISTS (
            SELECT 1
            FROM public.conflict c
            WHERE c.entry_id = public.conflict_candidate.entry_id
        )
    );

CREATE POLICY conflict_candidate_insert ON public.conflict_candidate
    FOR INSERT TO procurement_app, procurement_ingest
    WITH CHECK (true);

CREATE POLICY conflict_candidate_ingest_select ON public.conflict_candidate
    FOR SELECT TO procurement_ingest
    USING (true);

-- ---------------------------------------------------------------------------
-- Confidentiality for the queue (NFR-03, AC-8).
--
-- `conflict.explanation` is free text written to be read by a human -- "record
-- says 0.19 USD/W, web says 0.35" -- so it carries the disputed values
-- themselves, not a reference to them. Measured as procurement_app against a
-- document that role could not see:
--
--     SELECT entry_id, explanation FROM public.conflict;
--      cf-1 | CONFIDENTIAL: record says 0.19, web says 0.35
--
-- **Why this needs its own function rather than reusing document_is_restricted.**
-- `conflict` has no document_id, and cannot: an INTER_DOCUMENT conflict is by
-- definition about two or more documents. Its confidentiality is therefore the
-- OR over the documents of its candidate claims -- restricted if ANY candidate
-- comes from a restricted document, because the explanation quotes all of them.
--
-- That derivation cannot be a stored column filled at INSERT: `conflict` rows
-- are inserted before their `conflict_candidate` rows exist (the FK runs that
-- way round), so at INSERT time there is nothing to derive from. Computing it on
-- read is what makes it correct rather than merely convenient.
--
-- SECURITY DEFINER for the same reason as document_is_restricted: this walk
-- crosses `claim`, which is itself FORCE ROW LEVEL SECURITY as of 04_claim.sql,
-- and a lookup that cannot see restricted claims would report every conflict as
-- unrestricted -- failing open, in the one function whose job is to fail closed.
--
-- `conflict_candidate` now has FORCE RLS. Its SELECT policy inherits parent
-- `conflict` visibility via EXISTS, and must not call this function -- which
-- reads `conflict_candidate` -- or PostgreSQL rejects the query as recursive.
-- Owner/audit roles keep USING (true) so this SECURITY DEFINER walk still
-- sees every candidate under FORCE RLS. See the policies above.
CREATE FUNCTION public.conflict_is_restricted(p_entry_id text)
    RETURNS boolean
    LANGUAGE sql
    STABLE
    AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.conflict_candidate cc
        JOIN public.claim c ON c.claim_id = cc.claim_id
        WHERE cc.entry_id = p_entry_id
          AND public.document_is_restricted(c.document_id)
    );
$$ SECURITY DEFINER
   SET search_path = pg_catalog, pg_temp
   SET app.allow_restricted = 'true';

ALTER FUNCTION public.conflict_is_restricted(text) OWNER TO procurement_owner;
REVOKE EXECUTE ON FUNCTION public.conflict_is_restricted(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.conflict_is_restricted(text)
    TO procurement_app, procurement_ingest, procurement_owner, audit_owner;

ALTER TABLE public.conflict ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conflict FORCE ROW LEVEL SECURITY;

CREATE POLICY conflict_confidentiality_select ON public.conflict
    FOR SELECT
    USING (
        NOT public.conflict_is_restricted(entry_id)
        OR current_setting('app.allow_restricted', true) = 'true'
    );

CREATE POLICY conflict_write_insert ON public.conflict
    FOR INSERT
    WITH CHECK (true);

-- The UPDATE policy carries the confidentiality predicate, for the reason set
-- out at length in 03_chunk.sql: a permissive `USING (true)` plus an UPDATE that
-- needs no SELECT lets a role rewrite the lease and status of a queue item it
-- cannot read -- stealing or closing another reviewer's confidential conflict
-- blind. An entitled session claims it normally.
CREATE POLICY conflict_write_update ON public.conflict
    FOR UPDATE
    USING (
        NOT public.conflict_is_restricted(entry_id)
        OR current_setting('app.allow_restricted', true) = 'true'
    )
    WITH CHECK (true);

-- procurement_app cannot switch its own confidentiality off. See the equivalent
-- policy in 02_document.sql for the measured attack matrix, why this is a new
-- RESTRICTIVE policy rather than an edit to the permissive ones, and why
-- `WITH CHECK (true)` is spelled out.
--
-- Keyed on public.conflict_is_restricted(entry_id), the same derivation
-- conflict_confidentiality_select uses, because this table has no document_id
-- and cannot have one -- an INTER_DOCUMENT conflict is about several documents,
-- and its explanation quotes all of them.
--
-- No recursion, and the reason is worth stating because it is not obvious.
-- conflict_is_restricted() reads conflict_candidate and claim, and claim now
-- carries a RESTRICTIVE policy of its own (04_claim.sql). That policy is scoped
-- `TO procurement_app`, while the function runs SECURITY DEFINER as
-- procurement_owner, so it does not apply inside the function and the walk still
-- sees every candidate. Scope either policy to PUBLIC and this inverts: the walk
-- finds nothing, reports every conflict unrestricted, and fails *open* in the
-- one function whose job is to fail closed.
CREATE POLICY conflict_app_never_restricted ON public.conflict
    AS RESTRICTIVE FOR ALL TO procurement_app
    USING (NOT public.conflict_is_restricted(entry_id))
    WITH CHECK (true);

CREATE POLICY conflict_ingest_select ON public.conflict
    FOR SELECT TO procurement_ingest USING (true);
CREATE POLICY conflict_ingest_update ON public.conflict
    FOR UPDATE TO procurement_ingest USING (true) WITH CHECK (true);

GRANT SELECT, INSERT ON public.conflict TO procurement_ingest;
GRANT UPDATE (status, lease_owner, lease_expires_at, reopen_count)
    ON public.conflict TO procurement_ingest;
