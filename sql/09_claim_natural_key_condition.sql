-- Fold `condition` into `claim_natural_key`.
--
-- `04_claim.sql` originally keyed claims without it. Python
-- `FieldClaim.claim_key()` includes `condition.grouping_key()`, so the Sungrow
-- trio - one document, one field, one extractor, three ambients - is three
-- claims. The first production writer `ON CONFLICT`s this constraint, then
-- `_existing_claim_id` fails looking up the second ambient.
--
-- Greenfield applies pick this up from `04`. This file exists so a database
-- that already applied the old `04` is not stuck: that file is create-time
-- only and will not re-run. DROP + ADD is the whole migration; the new unique
-- matches the create-time definition, so a greenfield apply of `04` then `09`
-- is a no-op besides rewriting the same constraint.
--
-- Depends on: 04_claim.sql.

ALTER TABLE public.claim DROP CONSTRAINT claim_natural_key;

ALTER TABLE public.claim ADD CONSTRAINT claim_natural_key UNIQUE NULLS NOT DISTINCT (
    document_id, component_category, supplier, model, nameplate, field, extractor_version,
    condition
);

COMMENT ON CONSTRAINT claim_natural_key ON public.claim IS
    'C8 key plus component identity plus condition (D-1). Two ambients of one '
    'field from one extractor are two rows.';
