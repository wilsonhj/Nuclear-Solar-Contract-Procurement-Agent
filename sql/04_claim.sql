-- public.claim -- contract C1, the C8 invariant (tasks.md:31-46).
--
-- One immutable row per proposed field value: "workers propose, they do not
-- commit." The canonical value for a field is a projection over these rows,
-- computed by a reducer outside this schema. There is no UPDATE path here by
-- design, not merely by omission -- see the GRANT and trigger at the foot of
-- this file.
--
-- Depends on: 02_document.sql.

CREATE TABLE public.claim (
    claim_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- The document this claim is *about* -- i.e. which ingested document's
    -- component instance the field value describes. This is independent of
    -- where the evidence came from: a web-search-derived claim still names the
    -- document/component whose gap it fills, even though its own source_ref
    -- (below) may carry a URL rather than a document page. Do not conflate the
    -- two: source_ref->>'document_id' (the evidentiary reference, may be null)
    -- is a different value from this column (the claim's subject, never null).
    document_id        text NOT NULL REFERENCES public.document (document_id) ON DELETE RESTRICT,

    component_category text NOT NULL CHECK (component_category IN (
                           'pv_modules', 'inverters_pcs', 'trackers_mounting',
                           'transformers', 'cabling_wiring', 'combiner_boxes',
                           'bess', 'ems_scada'
                       )),
    supplier           text NOT NULL,
    model              text NOT NULL,
    -- Bin discriminator (component.py, ComponentInstance.nameplate: "one
    -- datasheet routinely covers several SKUs"). Nullable because not every
    -- category has one; see the note on the natural-key constraint below for
    -- what that means for collision-freedom on non-PV categories.
    nameplate          double precision,

    -- The frozen contract's own `key` column
    -- (contracts/canonical-parameters.md), e.g. `nameplate_power`. Not
    -- validated against the contract's field list at the DDL layer -- that
    -- check belongs with the application/test suite, the same way
    -- services/conflict_hitl/tolerance.py tests its own table against the
    -- contract rather than this schema re-deriving the list.
    field              text NOT NULL,
    extractor_version  text NOT NULL,

    -- CanonicalField's remaining six keys (schema/field.py): value, unit,
    -- verbatim_value, condition, source_tier, source_ref, confidence.
    -- conflict_status and resolution are deliberately absent here -- those
    -- belong to the canonical projection and the conflict queue, never to a
    -- raw claim.
    value              jsonb NOT NULL,
    unit               text,
    verbatim_value     text,
    -- schema.field.Condition, serialised whole, because Condition already
    -- treats its own field set generically (ConditionDimensions.model_fields)
    -- rather than one column per dimension -- mirroring that here means a new
    -- condition dimension needs no migration to this table.
    condition          jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_tier        text NOT NULL CHECK (source_tier IN ('system_of_record', 'web_supplement')),
    -- schema.field.SourceRef, serialised whole. May itself carry a document_id
    -- different from, or absent alongside, the outer document_id column above
    -- -- see the comment on that column.
    source_ref         jsonb NOT NULL,
    confidence         double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),

    extracted_at       timestamptz NOT NULL DEFAULT now(),

    -- A claim records that something WAS found; a field genuinely absent from a
    -- document produces no row at all (CellFlag.MISSING_DATA is a property of
    -- the canonical projection, not of an individual claim). This CHECK is the
    -- backstop for that design choice: a JSON null here would silently mean
    -- both things at once.
    CONSTRAINT claim_value_is_not_json_null CHECK (value <> 'null'::jsonb),

    -- The C8 key, extended. tasks.md:31 writes it literally as
    -- (document_id, field, extractor_version); taken that literally it
    -- collides across every SKU one multi-bin datasheet describes --
    -- component.py's own docstring: "a Trina TSM-NEG21C.20 sheet spans 6 bins
    -- and 22 CEC rows." The component-instance identity (category, supplier,
    -- model, nameplate) is added so this constraint means what C8 intends
    -- rather than what it literally says -- flagged in sql/README.md as an
    -- interpretation, not a literal reading, of a frozen contract.
    --
    -- NULLs in `nameplate` are collapsed via `NULLS NOT DISTINCT` so that
    -- retrying extraction for a component category without a bin discriminator
    -- (where `nameplate IS NULL`) deduplicates idempotently under `ON CONFLICT`
    -- rather than inserting duplicate rows on every retry.
    --
    -- `condition` is in the key because Python `FieldClaim.claim_key()` includes
    -- `grouping_key()`. Without it, one document / one field / one extractor
    -- stating three ambients (the Sungrow trio; D-1) collides, and the writer
    -- that `ON CONFLICT`s this constraint cannot persist them.
    --
    -- This also assumes extractor_version is fine-grained enough to
    -- distinguish genuinely different extraction strategies for the same
    -- field (e.g. WP-B B.6's field-guided vs document-guided cross-read), so
    -- that the two produce distinct claim rows rather than colliding under
    -- this key -- also flagged in sql/README.md.
    CONSTRAINT claim_natural_key UNIQUE NULLS NOT DISTINCT (
        document_id, component_category, supplier, model, nameplate, field, extractor_version,
        condition
    )
);

COMMENT ON TABLE public.claim IS
    'C1 core table, append-only per the C8 invariant (tasks.md:31). No '
    'UPDATE/DELETE is granted below; the trigger at the foot of this file is a '
    'secondary tripwire only, mirroring plan.md Decision 9''s reasoning for '
    'audit.event -- privilege separation is the actual boundary.';

CREATE INDEX claim_document_field_idx ON public.claim (document_id, field);
CREATE INDEX claim_supplier_model_idx ON public.claim (supplier, model);
CREATE INDEX claim_condition_gin_idx ON public.claim USING gin (condition jsonb_path_ops);

ALTER TABLE public.claim OWNER TO procurement_owner;

-- ---------------------------------------------------------------------------
-- Confidentiality (NFR-03, AC-8). `claim.value` and `claim.verbatim_value` hold
-- the extracted content of the document named by `document_id` -- the price, the
-- warranty term, the certification status -- so a claim row is exactly as
-- confidential as its document. Without the policies below, as procurement_app:
--
--     SELECT document_id FROM public.document WHERE document_id = 'doc-secret';
--      (0 rows)
--     SELECT document_id, field, value FROM public.claim
--       WHERE document_id = 'doc-secret';
--      doc-secret | price_per_watt_dc | 0.19
--
-- sql/README.md's decision 10 previously argued RLS here would be "inventing
-- scope Decision 3c never asked for". That reading holds only if `claim` were a
-- table of references; it is a table of extracted values, and AC-8 says content
-- must not influence a retrieved result *by any route*. The derivation needs no
-- new column and no C7 labelling model -- see public.document_is_restricted in
-- 02_document.sql.
ALTER TABLE public.claim ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.claim FORCE ROW LEVEL SECURITY;

CREATE POLICY claim_confidentiality_select ON public.claim
    FOR SELECT
    USING (
        NOT public.document_is_restricted(document_id)
        OR current_setting('app.allow_restricted', true) = 'true'
    );

-- RLS's default-deny is per command, so INSERT needs its own policy or the
-- GRANT below is starved. WITH CHECK (true): what a role may write is the
-- GRANT's business, exactly as on document/chunk. Note that this leaves
-- `INSERT ... RETURNING claim_id` failing for a restricted document under
-- procurement_app -- RLS applies the SELECT policy to the returned row -- and
-- claim_id is GENERATED ALWAYS AS IDENTITY, so conflict_candidate needs that
-- RETURNING. Writing claims about a confidential document is pipeline work and
-- belongs to procurement_ingest, which has the read-back policy below. This is
-- the same split, and the same reasoning, as 02_document.sql's.
CREATE POLICY claim_write_insert ON public.claim
    FOR INSERT
    WITH CHECK (true);

CREATE POLICY claim_ingest_select ON public.claim
    FOR SELECT TO procurement_ingest USING (true);

-- procurement_app cannot switch its own confidentiality off. See the equivalent
-- policy in 02_document.sql for the measured attack matrix, why this is a new
-- RESTRICTIVE policy rather than an edit to the permissive ones (they are OR'd;
-- this one is AND'd), and why `WITH CHECK (true)` is spelled out.
--
-- The predicate is public.document_is_restricted(document_id), derived rather
-- than stored, exactly as claim_confidentiality_select above expresses it -- a
-- claim is as confidential as the document it was extracted from, and this table
-- carries no flag of its own.
--
-- Note what this also closes, which the GUC escape alone did not explain.
-- `claim_ingest_select` is permissive and `USING (true)`, and RLS role matching
-- follows *inherited* membership: a mis-granted `GRANT procurement_ingest TO
-- procurement_app` therefore applies that policy to the application role with no
-- `SET ROLE` issued at all. Measured. Being AND'd, the policy below still denies
-- the row in that state -- so a membership mis-grant degrades from fail-open to
-- fail-closed. 00_roles.sql revokes the membership on every apply and
-- tests/test_sql_behaviour.py asserts it is absent; this is the layer that holds
-- if both of those are somehow bypassed.
CREATE POLICY claim_app_never_restricted ON public.claim
    AS RESTRICTIVE FOR ALL TO procurement_app
    USING (NOT public.document_is_restricted(document_id))
    WITH CHECK (true);

-- **These two exist so the append-only trigger keeps speaking.** The obvious
-- move on an append-only table is to declare no UPDATE/DELETE policy at all, so
-- a future mis-grant of those verbs finds zero eligible rows. Measured, that is
-- worse, because RLS filters rows out *before* a FOR EACH ROW trigger sees them:
--
--     SET ROLE procurement_owner;
--     UPDATE public.claim SET confidence = 0.1;   -- UPDATE 0     <- silent
--     DELETE FROM public.claim;                   -- DELETE 0     <- silent
--
-- The data survived, but the statement reported success, and plan.md Decision 9
-- picks the trigger tripwire precisely because it is *loud*: "a fork is loud,
-- not silent" is the same argument one table over. This is the empty-table trap
-- from sql/README.md in a new costume -- a FOR EACH ROW trigger cannot fire on
-- rows RLS has already removed, so a checklist run against an RLS-filtered table
-- proves exactly as little as one run against an empty one.
--
-- With `USING (true)` the rows stay eligible, the trigger fires, and the same
-- attempt raises. Nothing is loosened: no role is granted UPDATE or DELETE on
-- this table (see the GRANTs below), so these policies are reachable only in the
-- mis-grant case they exist to make audible.
CREATE POLICY claim_tripwire_update ON public.claim FOR UPDATE USING (true);
CREATE POLICY claim_tripwire_delete ON public.claim FOR DELETE USING (true);

-- No UPDATE/DELETE is granted: this is the actual "no UPDATE path" tasks.md's
-- C8 invariant asks for. The trigger further down is free extra insurance, not
-- the boundary -- a superuser or the table owner can still bypass it, exactly
-- as Decision 9 documents for audit.event.
GRANT SELECT, INSERT ON public.claim TO procurement_app;
GRANT SELECT, INSERT ON public.claim TO procurement_ingest;

-- Shared immutability tripwire, reused by resolution (06_resolution.sql) for
-- the same reasoning. Owned by procurement_owner, the owner of every table
-- that uses it; audit.event gets its own copy in the audit schema
-- (07_audit_event.sql) so that function ownership never crosses the
-- audit/core privilege boundary either.
CREATE FUNCTION public.reject_mutation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
    AS $$
BEGIN
    RAISE EXCEPTION
        'this table is append-only: % is rejected by trigger (secondary tripwire '
        'only; privilege separation above is the actual boundary -- plan.md '
        'Decision 9). ALTER TABLE ... DISABLE TRIGGER and '
        'session_replication_role=replica both bypass this trigger with no DDL '
        'trace at all.', TG_OP;
END;
$$;

ALTER FUNCTION public.reject_mutation() OWNER TO procurement_owner;

CREATE TRIGGER claim_no_mutation
    BEFORE UPDATE OR DELETE ON public.claim
    FOR EACH ROW
    EXECUTE FUNCTION public.reject_mutation();

-- TRUNCATE is a separate event from UPDATE and DELETE and is NOT caught by the
-- row-level trigger above: TRUNCATE fires no per-row triggers, so a
-- `FOR EACH ROW` trigger sees nothing at all. Review found `TRUNCATE public.claim
-- CASCADE` succeeding and taking `resolution` and `conflict_candidate` with it,
-- while sql/README.md claimed all three verbs were refused.
--
-- `ON DELETE RESTRICT` on the child FKs does not help either -- TRUNCATE CASCADE
-- truncates the children rather than deleting through the constraint.
--
-- Statement-level, because that is the only level TRUNCATE has. Same tripwire
-- caveat as everything else here: DISABLE TRIGGER and
-- session_replication_role=replica bypass it, and privilege separation above is
-- the actual boundary. plan.md Decision 9's attack matrix lists
-- `TRUNCATE | Trigger: blocked`, which is what this restores.
CREATE FUNCTION public.reject_truncate() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
    AS $$
BEGIN
    RAISE EXCEPTION
        'this table is append-only: TRUNCATE is rejected by trigger (secondary '
        'tripwire only; privilege separation above is the actual boundary -- '
        'plan.md Decision 9). Note TRUNCATE fires no row-level triggers, so the '
        'append-only FOR EACH ROW trigger on this table does not see it -- this '
        'statement-level trigger is the only thing that does.';
END;
$$;

ALTER FUNCTION public.reject_truncate() OWNER TO procurement_owner;

CREATE TRIGGER claim_no_truncate
    BEFORE TRUNCATE ON public.claim
    FOR EACH STATEMENT
    EXECUTE FUNCTION public.reject_truncate();
