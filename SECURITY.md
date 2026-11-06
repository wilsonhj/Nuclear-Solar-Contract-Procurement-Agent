# Security Policy

## Supported Versions & Current Status

`multi-agent-procurement-agent` is currently in **Phase 1 (Foundational Contracts, Hardened Schema, Audit Chain, and Vertical Slice)**. Only the latest commit on `main` receives security updates. See [`docs/current-state.md`](docs/current-state.md) for a subsystem-by-subsystem breakdown of what is implemented and verified today versus planned Phase 2 runtime integrations.

| Version / Branch | Supported |
| ---------------- | --------- |
| `main` (HEAD)    | Yes       |
| Older commits    | No        |

## Reporting a Vulnerability

**Do not report security vulnerabilities or include exploitable details in public GitHub issues, pull requests, or discussions.**

If you discover a vulnerability—especially anything involving Row-Level Security (RLS) bypasses, confidential document leakage across `procurement_app` / `procurement_ingest`, audit hash-chain forgery, or spreadsheet formula injection—please report it privately:

1. Use [GitHub Private Vulnerability Reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository if enabled, or contact the repository maintainer directly (consistent with [`CONTRIBUTING.md`](CONTRIBUTING.md)).
2. Include a minimal, synthetic reproduction (never attach real commercial contracts, pricing schedules, or supplier-confidential PDFs).
3. Allow reasonable time for triage and remediation before any public disclosure.

## Enforced Security Controls

The repository enforces defense-in-depth across the database, domain models, audit trail, configuration boundary, and Excel output layer:

- **PostgreSQL `FORCE ROW LEVEL SECURITY` on all 7 content tables**: `public.document`, `public.chunk`, `public.claim`, `public.conflict`, `public.resolution`, `audit.event`, and `public.job` all enforce both `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` (`sql/02`–`sql/08`). Every table pairs a permissive confidentiality `SELECT` policy with an explicit `AS RESTRICTIVE FOR ALL TO procurement_app` policy so `procurement_app` cannot declassify restricted rows even by setting `app.allow_restricted = 'true'` on its own connection.
- **Least-privilege, `NOBYPASSRLS` database roles**: `sql/00_roles.sql` provisions NOLOGIN owner roles (`procurement_owner`, `audit_owner`) and non-owner runtime roles (`procurement_app`, `procurement_ingest`) with `NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION` re-asserted unconditionally and verified against `pg_catalog.pg_roles`. `CREATE ON SCHEMA public` is revoked from `PUBLIC` (`sql/01_extensions_and_settings.sql`), and every SQL function pins an explicit `SET search_path`.
- **Fail-closed confidential `DocumentType` restriction (`RESTRICTED_DOCUMENT_TYPES`)**: `SourceDocument` (`src/procurement_agent/schema/component.py`) forces `access_restricted = True` whenever `document_type` is in `RESTRICTED_DOCUMENT_TYPES` (`contract_tos`, `purchase_order`, `pricing`, `terms_and_conditions`, `warranty`), including on `model_copy(update=...)` and `model_construct`, and `sql/02_document.sql` carries the same rule as the `document_confidential_types_restricted` CHECK so no writer can store one unrestricted.
- **RFC 8785 (JCS) SHA-256 per-document audit hash chain**: `src/procurement_agent/audit/` canonicalizes event preimages with RFC 8785 JSON Canonicalization Scheme (JCS), serializes per-document appends via transaction-scoped `pg_advisory_xact_lock(hashtext(stream)::bigint)`, and enforces database-level walkability (`UNIQUE NULLS NOT DISTINCT (stream, prev_hash)`, self-referential FK `audit_event_parent_exists`, and append-only mutation/truncate tripwire triggers).
- **Credential masking & endpoint validation**: `Settings` (`src/procurement_agent/config.py`) wraps database DSNs and API keys in `pydantic.SecretStr` so credentials never appear in `repr()`, logs, or tracebacks, and requires `llm_endpoint` and `embedding_endpoint` to be absolute `http(s)` URLs with a host and no embedded userinfo (`user:pass@host`).
- **Spreadsheet formula-injection neutralization (`_safe_excel_text`)**: All untrusted string cells written by `src/procurement_agent/services/output/__init__.py` are sanitized before writing to `.xlsx` so leading formula triggers (`=`, `+`, `-`, `@`, `|`, tab, CR/LF, vertical tab, form feed), including after leading whitespace, are neutralized with a leading apostrophe.

## Incident Response & Credential Rotation Checklist

If a credential leak, unauthorized database access, or audit-chain anomaly is suspected:

1. **Rotate `PROCUREMENT_DATABASE_URL` credentials**:
   - Rotate passwords for `procurement_app`, `procurement_ingest`, and operator roles in PostgreSQL (`ALTER ROLE ... WITH PASSWORD ...`), update secret stores, and terminate existing backend sessions via `pg_terminate_backend`.
2. **Rotate `PROCUREMENT_LLM_API_KEY` and external service tokens**:
   - Revoke the compromised API key at the provider/gateway, issue a new scoped key, and redeploy worker configuration.
3. **Re-assert role attributes and schema privileges**:
   - Re-apply `sql/00_roles.sql` and `sql/01_extensions_and_settings.sql` against the cluster to verify no role acquired `SUPERUSER`, `BYPASSRLS`, or unintended memberships.
4. **Verify the audit hash chain (`python -m procurement_agent.audit`)**:
   - Run the chain verifier as an operator identity entitled to restricted streams, with the connection string in the environment. `--dsn` is refused so the connection string cannot appear in the process list, and a connection as `procurement_app` is refused because RLS would hide every restricted stream from it:
     ```bash
     PROCUREMENT_DATABASE_URL="postgresql://..." python -m procurement_agent.audit
     ```
   - Inspect any reported `hash_mismatch`, `broken_link`, `seq_gap`, `bad_genesis`, or `payload_not_canonical` defects by `(stream, seq, event_id)` and cross-check against external write-once log sinks or backups.
