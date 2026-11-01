-- public.job -- plan.md Decision 1 / tasks.md WP-I I.1.
--
-- The stage state machine: a Postgres table plus a
-- `SELECT ... FOR UPDATE SKIP LOCKED` worker loop, not a workflow framework.
--
-- `stage` is keyed on orchestrator.Stage
-- (src/procurement_agent/orchestrator/__init__.py) verbatim -- the same
-- discipline services/conflict_hitl/tolerance.py uses for its own table,
-- keyed on the frozen field contract rather than an invented name. Six
-- values, no more, no fewer, and deliberately no await_human_resolution stage:
-- plan.md Decision 2 detaches the human gate from the pipeline entirely, and
-- orchestrator.Stage's own docstring says so explicitly.
--
-- Depends on: 02_document.sql.

CREATE TABLE public.job (
    job_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Nullable: compose_workbook is a whole-store operation with no single
    -- document subject (docs/agent-topology.md's own stage table marks it
    -- "none" for fan-out, for the same reason). Every other stage is
    -- document-scoped.
    document_id        text REFERENCES public.document (document_id) ON DELETE RESTRICT,

    stage              text NOT NULL CHECK (stage IN (
                           'ingest', 'extract', 'index', 'enrich_via_web',
                           'detect_conflicts', 'compose_workbook'
                       )),

    status             text NOT NULL DEFAULT 'pending' CHECK (status IN (
                           'pending', 'running', 'succeeded', 'failed', 'quarantined'
                       )),

    -- I.2: every stage must be independently idempotent; retries are
    -- at-least-once. This is the DB-level half of that promise, the same
    -- pattern as document.content_hash and claim's natural key: the enqueue
    -- path is `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`, so
    -- re-enqueuing the same logical unit of work is a no-op rather than a
    -- duplicate row racing the original.
    idempotency_key    text NOT NULL,
    payload            jsonb NOT NULL DEFAULT '{}'::jsonb,

    attempt            integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts       integer NOT NULL DEFAULT 5 CHECK (max_attempts >= 1),
    -- I.2 backoff: a job only becomes eligible for the worker loop's SELECT
    -- once this is reached. Defaults to now() so a freshly enqueued job is
    -- immediately eligible.
    next_attempt_at    timestamptz NOT NULL DEFAULT now(),

    -- D-12e: 15-minute lease, the same duration WP-F F.1 uses for conflict
    -- claiming -- tasks.md never gives the job table its own number, so this
    -- reuses D-12e's rather than inventing a second one; flagged in
    -- sql/README.md. Swept back to 'pending' by a periodic worker-loop query
    -- (see the comment on the supporting index below), not a DB trigger --
    -- Decision 1 keeps this logic in the runner, not in the schema.
    lease_owner        text,
    lease_expires_at   timestamptz,

    last_error         text,

    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT job_idempotency_key_unique UNIQUE (idempotency_key),
    CONSTRAINT job_lease_fields_consistent
        CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

COMMENT ON TABLE public.job IS
    'Stage state machine for plan.md Decision 1. No corresponding Pydantic '
    'model exists in schema/ -- this table is this file''s own C8-consistent '
    'design, not a mapping of an existing frozen type.';

-- The worker loop's hot path: claim the next eligible job for a stage.
--   SELECT job_id FROM job
--     WHERE stage = $1 AND status = 'pending' AND next_attempt_at <= now()
--     ORDER BY created_at
--     FOR UPDATE SKIP LOCKED
--     LIMIT $2;
CREATE INDEX job_pending_by_stage_idx ON public.job (stage, next_attempt_at)
    WHERE status = 'pending';

-- The lease sweeper's hot path:
--   UPDATE job SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL
--     WHERE status = 'running' AND lease_expires_at < now();
CREATE INDEX job_running_lease_idx ON public.job (lease_expires_at)
    WHERE status = 'running';

CREATE INDEX job_document_id_idx ON public.job (document_id);

CREATE FUNCTION public.job_touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
    AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;

-- Bookkeeping only, not a business-rule trigger of the kind Decision 9 warns
-- against: it does not enforce or hide any invariant, it only timestamps a
-- change that already happened.
CREATE TRIGGER job_set_updated_at
    BEFORE UPDATE ON public.job
    FOR EACH ROW
    EXECUTE FUNCTION public.job_touch_updated_at();

ALTER TABLE public.job OWNER TO procurement_owner;
ALTER FUNCTION public.job_touch_updated_at() OWNER TO procurement_owner;

-- Confidentiality (NFR-03, AC-8). `job.payload` is the stage's own input -- for
-- `extract` or `enrich_via_web` that is document content, and it was readable as
-- procurement_app for a document that role could not see:
--
--     SELECT job_id, document_id, payload FROM public.job;
--      1 | doc-secret | {"secret": "0.19"}
--
-- `last_error` is the same hazard by a different route: an exception message
-- routinely quotes the value that failed to parse.
--
-- `document_id` is nullable here (compose_workbook is a whole-store stage with
-- no single subject), and public.document_is_restricted returns false for NULL,
-- so those rows stay visible -- correct, since a whole-store job's payload names
-- no document.
ALTER TABLE public.job ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.job FORCE ROW LEVEL SECURITY;

CREATE POLICY job_confidentiality_select ON public.job
    FOR SELECT
    USING (
        NOT public.document_is_restricted(document_id)
        OR current_setting('app.allow_restricted', true) = 'true'
    );

CREATE POLICY job_write_insert ON public.job
    FOR INSERT
    WITH CHECK (true);

-- The confidentiality predicate is in USING, not `true`, for the reason
-- 03_chunk.sql sets out: an UPDATE needs no SELECT, so a permissive USING would
-- let a role quarantine, re-lease or fail a restricted document's job blind --
-- a denial-of-service on exactly the pipeline NFR-03 is protecting.
CREATE POLICY job_write_update ON public.job
    FOR UPDATE
    USING (
        NOT public.document_is_restricted(document_id)
        OR current_setting('app.allow_restricted', true) = 'true'
    )
    WITH CHECK (true);

-- procurement_app cannot switch its own confidentiality off. See the equivalent
-- policy in 02_document.sql for the measured attack matrix, why this is a new
-- RESTRICTIVE policy rather than an edit to the permissive ones, and why
-- `WITH CHECK (true)` is spelled out.
--
-- Keyed on public.document_is_restricted(document_id), matching
-- job_confidentiality_select above. `document_id` is nullable here
-- (compose_workbook is a whole-store stage with no single subject) and the
-- helper returns false for NULL, so whole-store jobs stay visible -- correct,
-- since such a job's payload names no document. `NOT false` is `true`, so the
-- restrictive predicate admits them too; it does not accidentally hide the
-- one stage that has nothing to hide.
CREATE POLICY job_app_never_restricted ON public.job
    AS RESTRICTIVE FOR ALL TO procurement_app
    USING (NOT public.document_is_restricted(document_id))
    WITH CHECK (true);

CREATE POLICY job_ingest_select ON public.job
    FOR SELECT TO procurement_ingest USING (true);
CREATE POLICY job_ingest_update ON public.job
    FOR UPDATE TO procurement_ingest USING (true) WITH CHECK (true);

-- job is the mutable state machine itself -- unlike claim/resolution/
-- audit.event, UPDATE is granted rather than withheld, since state transitions
-- are the table's entire purpose. No DELETE: nothing in the spec ever removes a
-- job row -- a poison message is quarantined (status = 'quarantined', I.4),
-- never deleted.
--
-- **Column-level, not full-table.** The first version granted plain
-- `UPDATE ON public.job`, which is every column, and `document`/`conflict` in
-- this same file set had already established the opposite discipline for the
-- same reason. What that cost, measured as procurement_app:
--
--     UPDATE public.job SET idempotency_key = 'HIJACKED';   -- UPDATE 1
--     UPDATE public.job SET stage = 'ingest', document_id = 'doc-open',
--                          created_at = now();              -- UPDATE 1
--
-- `idempotency_key` is the whole of I.2's at-least-once guarantee: rewriting it
-- makes `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` stop deduplicating,
-- so the retry it exists to absorb becomes a second live job racing the first --
-- and the same buggy worker that corrupts the key is the one whose retries the
-- key was protecting against. `stage`, `document_id` and `payload` define which
-- unit of work this row *is*, not how far it has got; `created_at` is the
-- worker loop's FIFO ordering key; `updated_at` is the trigger's to write, and
-- withholding it from the grant does not stop the trigger, because column
-- privileges are checked against the statement's target list and not against
-- what a BEFORE trigger assigns to NEW (verified against a live server).
--
-- What remains is exactly the state machine: status, the retry counter and its
-- backoff, the lease pair, and the error text.
GRANT SELECT, INSERT ON public.job TO procurement_app;
GRANT UPDATE (status, attempt, next_attempt_at, lease_owner, lease_expires_at, last_error)
    ON public.job TO procurement_app;

GRANT SELECT, INSERT ON public.job TO procurement_ingest;
GRANT UPDATE (status, attempt, next_attempt_at, lease_owner, lease_expires_at, last_error)
    ON public.job TO procurement_ingest;
