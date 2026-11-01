-- public.chunk -- contract C1.
--
-- The retrieval unit produced by WP-C indexing (FR-RAG-01/02/05, plan.md
-- Decisions 3a/3b/5/6).
--
-- Depends on: 01_extensions_and_settings.sql (vector, pg_trgm), 02_document.sql.

-- `tsv` is generated with the TWO-argument to_tsvector(regconfig, text), which
-- PostgreSQL's own catalog marks IMMUTABLE (pg_proc.provolatile = 'i'), so it is
-- legal directly inside GENERATED ALWAYS AS ... STORED. Verified against a live
-- PostgreSQL 17.10: the two-argument form creates the table, and the ONE-argument
-- to_tsvector(text) is the STABLE one that fails with "generation expression is
-- not immutable" -- because it reads default_text_search_config at runtime.
--
-- Pinning the configuration in the call is therefore the whole fix; no IMMUTABLE
-- wrapper function is needed. An earlier draft of this file added one, on the
-- mistaken belief that the two-argument form was STABLE. Worth naming, because
-- wrapping a genuinely STABLE function and declaring the wrapper IMMUTABLE is
-- not a harmless workaround -- it lies to the planner and can silently corrupt
-- any index built on it. The same immutability is why the standard
-- `CREATE INDEX ... USING gin(to_tsvector('english', col))` idiom works at all.

CREATE TABLE public.chunk (
    chunk_id           text PRIMARY KEY,
    document_id        text NOT NULL REFERENCES public.document (document_id) ON DELETE RESTRICT,

    -- Decision 6: a table is indexed three times, all three rows pointing at
    -- one table_id. `prose` is this schema's own name for the non-table case;
    -- plan.md only names the three table_* kinds.
    chunk_kind         text NOT NULL CHECK (chunk_kind IN (
                           'prose', 'table_full', 'table_row', 'table_summary'
                       )),
    table_id           text,

    -- Denormalized onto the chunk row per FR-RAG-02's own list of what indexing
    -- must write: "doc ID, chunk ID, component category, supplier, doc type,
    -- page, source URI, timestamps, source-tier flag" (see
    -- services/indexing/__init__.py). `model` and `access_restricted` are added
    -- here beyond that literal list, for symmetry with supplier and with
    -- document.access_restricted respectively -- flagged in sql/README.md.
    -- Denormalized rather than joined so VectorStorePort.search's category and
    -- supplier filters (ports/__init__.py) never need a join against claim on
    -- the hot retrieval path.
    component_category text CHECK (component_category IN (
                           'pv_modules', 'inverters_pcs', 'trackers_mounting',
                           'transformers', 'cabling_wiring', 'combiner_boxes',
                           'bess', 'ems_scada'
                       )),
    supplier           text,
    model              text,
    document_type      text CHECK (document_type IN (
                           'contract_tos',
                           'purchase_order',
                           'environmental_regulation',
                           'terms_and_conditions',
                           'warranty',
                           'technical_documentation',
                           'pricing',
                           'spec_sheet'
                       )),
    page               integer,
    section            text,
    source_uri         text,

    -- FR-RAG-03: system-of-record chunks must remain distinguishable from web
    -- supplements at all times, so this travels with every retrieved row rather
    -- than being looked up later (see ports.RetrievedChunk).
    source_tier        text NOT NULL CHECK (source_tier IN ('system_of_record', 'web_supplement')),
    access_restricted  boolean NOT NULL DEFAULT false,

    -- The verbatim chunk text: what a citation shows a reviewer, and what the
    -- lexical indexes below run over. Decision 3b's motivating failure
    -- ("JKM610N-66HL4M-V" not matching "JKM610N 66HL4M V") lives in source
    -- text, not in a generated context sentence, which is why the indexes
    -- target this column and not context_prefix.
    chunk_text         text NOT NULL,

    -- Decision 6, "contextual retrieval": 1-2 sentences of document/section
    -- context, prepended only to the text that gets embedded. Kept in its own
    -- column, separate from chunk_text, so a citation never presents
    -- LLM-generated framing to a reviewer as if it were the source. This split
    -- is this file's own resolution of an ambiguity Decision 6 leaves open --
    -- see sql/README.md. NULL where no prefix was generated.
    context_prefix     text,

    -- Decision 5: Qwen3-Embedding-4B, Matryoshka-truncated and renormalised to
    -- 1024 dims. The truncation is load-bearing, not an optimisation: pgvector
    -- caps HNSW indexable dimensions at 2000, so the native 2560 could never be
    -- indexed even if Decision 3a is later revisited.
    embedding          vector(1024) NOT NULL,

    -- Decision 3b's lexical leg. Generated, not written by application code, so
    -- there is no code path that could forget to keep it in sync with
    -- chunk_text, with the configuration pinned in the call -- see the note above
    -- on why the two-argument form is the one that belongs here.
    tsv                tsvector GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED NOT NULL,

    created_at         timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.chunk IS
    'C1 core table. FR-RAG-05: incremental add/update/delete by stable chunk_id, '
    'never a full re-index -- see ports.VectorStorePort.upsert and .delete.';

COMMENT ON COLUMN public.chunk.embedding IS
    'Decision 3a: no ANN index is built on this column anywhere in this schema. '
    'Measured at 50,000 chunks with a 1 percent selective filter and LIMIT 10: '
    'HNSW with hnsw.iterative_scan=off silently returned 5 rows, no error, no '
    'warning; HNSW against an ACL-array GIN filter returned 0. Exact search '
    'combined with a metadata filter measured 3.5 to 5.5 ms with guaranteed '
    'top-k; the unfiltered worst case was 130 to 180 ms, trivial beside the LLM '
    'call it feeds. Do not add a hnsw or ivfflat index here on the strength of '
    'FR-RAG-02''s ANN wording alone -- plan.md Decision 3a supersedes it. If a '
    'future migration adds one anyway, hnsw.iterative_scan is already set to '
    'relaxed_order (01_extensions_and_settings.sql), and pgvector enforces '
    'ef_construction >= 2 * m as a hard error at index build time, not merely '
    'README guidance.';

-- Lookup and filter indexes. No index is created on `embedding` -- see the
-- comment on that column above.
CREATE INDEX chunk_document_id_idx ON public.chunk (document_id);
CREATE INDEX chunk_tsv_idx ON public.chunk USING gin (tsv);
CREATE INDEX chunk_text_trgm_idx ON public.chunk USING gin (chunk_text gin_trgm_ops);
CREATE INDEX chunk_category_supplier_idx ON public.chunk (component_category, supplier);
CREATE INDEX chunk_table_id_idx ON public.chunk (table_id) WHERE table_id IS NOT NULL;

ALTER TABLE public.chunk OWNER TO procurement_owner;

-- See the equivalent comment in 02_document.sql: FORCE applies RLS uniformly,
-- including to procurement_owner, so the policies below are deliberately not
-- scoped `TO procurement_app`.
ALTER TABLE public.chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chunk FORCE ROW LEVEL SECURITY;

CREATE POLICY chunk_confidentiality_select ON public.chunk
    FOR SELECT
    USING (
        NOT access_restricted
        OR current_setting('app.allow_restricted', true) = 'true'
    );

-- Same reasoning as document.sql's write-path policies: RLS's default-deny is
-- per command, so INSERT/UPDATE/DELETE each need their own permissive policy or
-- the SELECT-only policy above would leave them at zero rows despite the GRANT
-- below allowing them.
--
-- **The UPDATE and DELETE policies carry the confidentiality predicate.** The
-- first version used `USING (true)` on both, on the reasoning that RLS is for
-- retrieval-time visibility and the write path should sit exactly where the
-- GRANT puts it. Review showed what that costs: a permissive `USING (true)` is
-- OR'd with the SELECT policy, a WHERE-less UPDATE needs no SELECT, and
-- `access_restricted` was inside the app's own UPDATE column set. So
--
--     UPDATE public.chunk SET access_restricted = false;
--
-- ran as procurement_app, returned `UPDATE 2`, and declassified a row that role
-- could not read one statement earlier. `DELETE FROM public.chunk` likewise
-- destroyed a row it could not see. Decision 3c's basis is "RLS never leaked a
-- row in any test" -- the application role, which is the principal the control
-- exists to contain, defeated it in one statement.
--
-- With the predicate in `USING`, a restricted row is not a candidate for UPDATE
-- or DELETE unless the session has set `app.allow_restricted` -- i.e. unless it
-- is already entitled to see the row. Correcting a misclassification stays
-- possible for an entitled session, which is the case the narrow column grant
-- was written for; it just is no longer possible blind.
CREATE POLICY chunk_write_insert ON public.chunk
    FOR INSERT
    WITH CHECK (true);

CREATE POLICY chunk_write_update ON public.chunk
    FOR UPDATE
    USING (
        NOT access_restricted
        OR current_setting('app.allow_restricted', true) = 'true'
    )
    WITH CHECK (true);

CREATE POLICY chunk_write_delete ON public.chunk
    FOR DELETE
    USING (
        NOT access_restricted
        OR current_setting('app.allow_restricted', true) = 'true'
    );

-- procurement_app cannot switch its own confidentiality off. Every policy above
-- admits `current_setting('app.allow_restricted', true) = 'true'`, and the
-- application role sets that GUC itself -- including from the connection string,
-- before any statement is issued. Permissive policies are OR'd, so no addition
-- to the set above can narrow it; `AS RESTRICTIVE` is AND'd, which is why this
-- is a new policy rather than an edit. The full argument, the measured attack
-- matrix, and why `WITH CHECK (true)` is spelled out rather than defaulted are
-- on the equivalent policy in 02_document.sql -- read that one first.
--
-- This table's predicate is its own `access_restricted` column rather than a
-- derivation helper, matching chunk_confidentiality_select above: a chunk may be
-- *more* restricted than its parent, so the flag is stored here, maintained by
-- the inheritance trigger at the foot of this file.
--
-- The trigger and this policy compose in the safe direction. The trigger raises
-- `access_restricted` on the NEW row before the check runs, and `WITH CHECK
-- (true)` accepts the raised value, so procurement_app indexing a restricted
-- document still writes the chunk -- and then cannot read it back. Reverse the
-- `WITH CHECK` to the defaulted `USING` and that insert fails instead, which is
-- again the safe action becoming the failing one.
CREATE POLICY chunk_app_never_restricted ON public.chunk
    AS RESTRICTIVE FOR ALL TO procurement_app
    USING (NOT access_restricted)
    WITH CHECK (true);

-- Retrieval principal: SELECT only. Incremental add/update/delete of chunks
-- (FR-RAG-05) is `procurement_ingest`'s job; granting those verbs to the
-- application role would let it mutate retrieval units it can see and probe
-- ones it cannot except through RLS. The write policies below stay as
-- defence-in-depth if this GRANT is ever widened.
GRANT SELECT ON public.chunk TO procurement_app;

-- The indexing principal (00_roles.sql, procurement_ingest); see the long
-- comment on the equivalent policies in 02_document.sql for why this role
-- exists and why these four policies are the one role-scoped set in the schema.
--
-- All four verbs, not just INSERT: FR-RAG-05 requires incremental add/update/
-- delete by stable chunk_id and never a full re-index, and re-indexing a
-- reclassified confidential document is exactly the case procurement_app's
-- confidentiality-carrying UPDATE/DELETE policies (correctly) refuse. Without a
-- SELECT policy here the indexer could not even run `INSERT ... RETURNING
-- chunk_id` for a restricted document's chunks -- and by the inheritance
-- trigger at the foot of this file, every chunk of a restricted document is
-- restricted, so that is all of them.
CREATE POLICY chunk_ingest_select ON public.chunk
    FOR SELECT TO procurement_ingest USING (true);
CREATE POLICY chunk_ingest_insert ON public.chunk
    FOR INSERT TO procurement_ingest WITH CHECK (true);
CREATE POLICY chunk_ingest_update ON public.chunk
    FOR UPDATE TO procurement_ingest USING (true) WITH CHECK (true);
CREATE POLICY chunk_ingest_delete ON public.chunk
    FOR DELETE TO procurement_ingest USING (true);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.chunk TO procurement_ingest;

-- ---------------------------------------------------------------------------
-- chunk.access_restricted is DERIVED from the parent document, not merely
-- declared alongside it.
--
-- The column above defaults to false and, in the first version, nothing tied it
-- to document.access_restricted. Review demonstrated the consequence: an
-- indexer that writes a chunk without setting the flag leaves the text
-- world-readable while the parent document is correctly hidden --
--
--     select chunk_id, chunk_text from public.chunk;
--      c3 | CONFIDENTIAL: price 0.19/W from restricted pricing doc
--     select document_id from public.document;
--      (0 rows)
--
-- Chunks are the retrieval unit: they are what feeds citations and the LLM
-- context window. So this is the table where NFR-03 and AC-8 are actually
-- decided, and a flag the caller may simply forget is not a control.
--
-- tasks.md:53 names this exact failure in advance: "C7 is a single decision
-- constraining two work packages at opposite ends of the pipeline (labelling at
-- ingest, enforcement at retrieval). This is the most common place this kind of
-- plan breaks."
--
-- The rule is OR, not assignment: a chunk is at least as restricted as its
-- parent, and may be *more* restricted (a confidential paragraph inside an
-- otherwise open datasheet). Restriction can therefore only ever increase, and
-- forgetting the flag is safe rather than silent.
--
-- BEFORE INSERT OR UPDATE so it also holds when a document is reclassified and
-- its chunks are re-labelled, and so an UPDATE cannot lower a chunk below its
-- parent.
CREATE FUNCTION public.chunk_inherit_access_restricted() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    parent_restricted boolean;
BEGIN
    -- The lookup must see restricted parents, and neither SECURITY DEFINER nor
    -- ownership grants that: `document` is FORCE ROW LEVEL SECURITY, which
    -- applies to procurement_owner exactly as it does to anyone else. A first
    -- version of this function relied on SECURITY DEFINER alone and failed
    -- closed and loudly on the very first restricted chunk --
    --
    --     ERROR: chunk.document_id doc2 has no document row
    --
    -- because the parent SELECT returned zero rows rather than `true`. Loud is
    -- the right direction to fail, but it made every restricted chunk
    -- un-insertable.
    --
    -- The `SET app.allow_restricted` clause on the function below is what
    -- actually grants visibility, using this schema's own confidentiality
    -- mechanism rather than a new privilege: the setting is scoped to this
    -- function invocation and reverts on exit, so it cannot leak into the
    -- calling session.
    SELECT d.access_restricted INTO parent_restricted
    FROM public.document d
    WHERE d.document_id = NEW.document_id;

    IF parent_restricted IS NULL THEN
        -- The FK below rejects this anyway; failing closed here means the
        -- ordering of constraint and trigger cannot open a window.
        RAISE EXCEPTION
            'chunk.document_id % has no document row; cannot derive '
            'access_restricted', NEW.document_id;
    END IF;

    NEW.access_restricted := NEW.access_restricted OR parent_restricted;
    RETURN NEW;
END;
$$ SECURITY DEFINER
   SET search_path = pg_catalog, pg_temp
   SET app.allow_restricted = 'true';

-- SECURITY DEFINER is deliberate and is the one such function in this schema.
-- The body reads exactly one boolean from one table by primary key, takes no
-- caller-supplied SQL, and returns nothing to the caller, so the usual
-- SECURITY DEFINER hazard -- privilege escalation through an injectable body or
-- a mutable search_path -- does not apply. search_path is pinned per the
-- standard hardening, and EXECUTE is revoked from PUBLIC so it is reachable
-- only as this trigger.
--
-- Note what the `SET app.allow_restricted` clause can and cannot do: it makes
-- the parent lookup succeed, and it cannot be used to read anything else,
-- because the function returns only `NEW` and the one boolean never leaves it.
ALTER FUNCTION public.chunk_inherit_access_restricted() OWNER TO procurement_owner;
REVOKE EXECUTE ON FUNCTION public.chunk_inherit_access_restricted() FROM PUBLIC;

CREATE TRIGGER chunk_inherit_access_restricted
    BEFORE INSERT OR UPDATE ON public.chunk
    FOR EACH ROW
    EXECUTE FUNCTION public.chunk_inherit_access_restricted();
