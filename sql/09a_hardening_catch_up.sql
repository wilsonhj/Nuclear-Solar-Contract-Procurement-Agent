-- Bring a database that already applied 01-09 up to the hardened definitions.
--
-- 01, 02, 03, 04, 05, 07, 08 and 09 were edited in place (schema privileges,
-- function search_path, chunk write grants, conflict_candidate RLS, audit
-- recorded_at default, NULLS NOT DISTINCT natural key, confidential document
-- CHECK). Those files are create-time only and will not re-run on an existing
-- database, the reason 09 exists. This file is the forward-only catch-up, and
-- every statement is idempotent, so a greenfield apply of 00-09 then this file
-- changes nothing.
--
-- Named 09a rather than 10 because D-27 assigns sql/10 to the claim-resolution
-- link; `09_` sorts before `09a_`, so apply order is unchanged.
--
-- Depends on: 00-09.

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO procurement_owner, audit_owner, procurement_app, procurement_ingest;

ALTER FUNCTION public.document_is_restricted(text) SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.chunk_inherit_access_restricted() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.reject_mutation() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.reject_truncate() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.conflict_is_restricted(text) SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION audit.reject_mutation() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION audit.reject_truncate() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.job_touch_updated_at() SET search_path = pg_catalog, pg_temp;

REVOKE INSERT, UPDATE, DELETE ON public.chunk FROM procurement_app;

ALTER TABLE audit.event ALTER COLUMN recorded_at DROP DEFAULT;

ALTER TABLE public.claim DROP CONSTRAINT claim_natural_key;
ALTER TABLE public.claim ADD CONSTRAINT claim_natural_key UNIQUE NULLS NOT DISTINCT (
    document_id, component_category, supplier, model, nameplate, field, extractor_version,
    condition
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint
         WHERE conname = 'document_confidential_types_restricted'
    ) THEN
        ALTER TABLE public.document ADD CONSTRAINT document_confidential_types_restricted
            CHECK (
                access_restricted OR document_type NOT IN (
                    'contract_tos', 'purchase_order', 'pricing', 'terms_and_conditions',
                    'warranty'
                )
            );
    END IF;
END
$$;

ALTER TABLE public.conflict_candidate ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conflict_candidate FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies
                    WHERE tablename = 'conflict_candidate'
                      AND policyname = 'conflict_candidate_owner') THEN
        CREATE POLICY conflict_candidate_owner ON public.conflict_candidate
            FOR ALL TO procurement_owner, audit_owner USING (true) WITH CHECK (true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies
                    WHERE tablename = 'conflict_candidate'
                      AND policyname = 'conflict_candidate_select') THEN
        CREATE POLICY conflict_candidate_select ON public.conflict_candidate
            FOR SELECT TO procurement_app
            USING (EXISTS (SELECT 1 FROM public.conflict c
                            WHERE c.entry_id = public.conflict_candidate.entry_id));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies
                    WHERE tablename = 'conflict_candidate'
                      AND policyname = 'conflict_candidate_insert') THEN
        CREATE POLICY conflict_candidate_insert ON public.conflict_candidate
            FOR INSERT TO procurement_app, procurement_ingest WITH CHECK (true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies
                    WHERE tablename = 'conflict_candidate'
                      AND policyname = 'conflict_candidate_ingest_select') THEN
        CREATE POLICY conflict_candidate_ingest_select ON public.conflict_candidate
            FOR SELECT TO procurement_ingest USING (true);
    END IF;
END
$$;
