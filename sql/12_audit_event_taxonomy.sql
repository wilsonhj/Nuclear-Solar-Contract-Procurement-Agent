-- audit.event amendments for C4 (P2-C7): the taxonomy, and a self-parent CHECK.
--
-- phase-2/story-4-persistence-runner.md section A.4; clarifications.md D-13.
-- Forward-only and idempotent, like 09a: re-running this file changes nothing.
--
-- 1. `web_search` LEAVES audit.event's taxonomy.
--
--    It was in sql/07's CHECK and could never be stored there. A gap-triggered
--    web search exists because no document supplied the value, and every
--    audit.event row needs `document_id NOT NULL REFERENCES public.document`
--    (A-49). sql/11 gives it a home, so here it is removed.
--
--    D-13 section 5 forbids removing a value "once any event exists", because a
--    chain cannot tolerate a value it already holds becoming illegal. That is
--    exactly the condition this file checks first, and it holds structurally,
--    not by luck: the FK above makes a `web_search` row unrepresentable, so
--    none can exist in any deployment. The DO block makes the check explicit
--    and readable; the ADD CONSTRAINT after it would refuse a violating row
--    regardless, because constraint validation scans the table without regard
--    to row-level security.
--
-- 2. `recorded_at` has no DEFAULT. 09a already dropped it; the statement is
--    repeated because P2-C7 assigns it to this file and it is a no-op where
--    09a ran. D-13 section 4: a defaulted timestamp is unhashable by the caller.
--
-- 3. An event may not be its own parent. A row whose prev_hash equals its own
--    hash satisfies sql/07's self-referencing FK by itself, so every
--    constraint passed and procurement_app -- the INSERT-only role -- could
--    write one. Planted at a high `seq`, it is what the writer's tip read
--    (`ORDER BY seq DESC LIMIT 1`) returns, and every honest append afterwards
--    chains off it; the genuine chain is orphaned. `audit.verify` reports such
--    a row as a broken link, because it walks in `seq` order rather than by
--    following prev_hash, so the plant is *detected* -- but only after it has
--    redirected every later write. This makes it impossible to write instead.
--
-- Depends on: 07_audit_event.sql, 09a_hardening_catch_up.sql, 11_audit_run_event.sql.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM audit.event WHERE event_type = 'web_search') THEN
        RAISE EXCEPTION
            'audit.event holds web_search rows, so the value cannot leave its '
            'taxonomy: D-13 section 5 forbids removing a value any event uses. '
            'This should be impossible (the document FK makes such a row '
            'unrepresentable); investigate before applying sql/12.';
    END IF;
END;
$$;

-- `event_event_type_check` is PostgreSQL's generated name for sql/07's inline
-- CHECK. Dropped by that name and by the explicit name below, so a re-run finds
-- nothing to drop and re-adds the same definition.
ALTER TABLE audit.event DROP CONSTRAINT IF EXISTS event_event_type_check;
ALTER TABLE audit.event DROP CONSTRAINT IF EXISTS audit_event_event_type_check;
ALTER TABLE audit.event ADD CONSTRAINT audit_event_event_type_check CHECK (event_type IN (
    'document_ingested',
    'parse_failure',
    'extraction',
    'conflict_detected',
    'resolution',
    'attempt_failed'
));

ALTER TABLE audit.event ALTER COLUMN recorded_at DROP DEFAULT;

ALTER TABLE audit.event DROP CONSTRAINT IF EXISTS audit_event_no_self_parent;
ALTER TABLE audit.event ADD CONSTRAINT audit_event_no_self_parent
    CHECK (prev_hash IS DISTINCT FROM hash);
