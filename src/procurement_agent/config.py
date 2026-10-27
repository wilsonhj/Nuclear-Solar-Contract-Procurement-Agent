"""Runtime configuration.

The TRS implies a config surface without specifying values: supplier row-vs-column
orientation (FR-OUT-01), the HITL confidence threshold (FR-ING-10), the numeric
conflict tolerance (FR-WEB-04), chunking parameters (FR-RAG-01) and endpoint
selection (section 6). Defaults below are starting points, not spec values -
see docs/open-questions.md.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schema import Severity

#: The `job` table's claim lease, in seconds: D-12e's 15 minutes, which `sql/08_job.sql`
#: restates in a comment rather than a constraint. It is the OUTERMOST bound in this
#: file - a worker whose stage outlives its lease is swept back to `pending` and
#: re-run by a second worker while the first is still writing. `tests/test_settings_timeouts.py`
#: reads D-12e so this cannot drift from the decision it mirrors.
JOB_LEASE_SECONDS: float = 15 * 60.0

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PROCUREMENT_",
        env_file=".env",
        extra="ignore",
        # The compose-gate threshold is a safety interlock, and a bound checked
        # only at construction is bypassed by plain attribute assignment.
        validate_assignment=True,
    )

    # --- Endpoints (swappable per NFR-04, self-hosted for confidential data per NFR-03) ---
    #
    # The bearer tokens and the DSNs are `SecretStr`; the endpoint and model names
    # beside them are not. A `SecretStr` renders as `**********` in `repr()`,
    # `str()`, an f-string and both `model_dump` forms, and yields its value only
    # to an explicit `get_secret_value()`.
    #
    # **This is a precaution, not a fix for an observed leak.** Nothing in `src/`
    # reads these five fields today and `Settings` has no export path - nothing
    # serves it over a wire, and no audit payload embeds it, so unlike Kedro's
    # session snapshot there is no boundary it currently crosses. What the plain
    # `str` gave away was the *default*: a credential rendered itself into any
    # log line, exception message or hashed payload that ever touched the object,
    # with nothing at the type level to make that a decision. Wrapping them now
    # is cheap precisely because there are no readers to update; wrapping them
    # after WP-A wires a real endpoint means auditing every call site instead.
    #
    # The split is deliberate rather than blanket. A DSN carries a password in
    # its userinfo and an API key authorises calls; an endpoint URL and a model
    # name authorise nothing and belong in a log line, where masking them would
    # cost debuggability and buy nothing. `tests/test_settings_secrets.py`
    # enforces the rule by *name shape* rather than against a list, so a
    # credential-shaped field added later arrives protected instead of arriving
    # plain and waiting to be noticed - the failure `services/confidence` already
    # shipped, where `ul_listing` fell out of a hand-written roster.
    llm_endpoint: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    embedding_endpoint: str | None = None
    embedding_model: str | None = None

    database_url: SecretStr | None = None
    vector_store_url: SecretStr | None = None
    object_store_url: SecretStr | None = None
    web_search_api_key: SecretStr | None = None

    # --- Review routing ---
    # There is deliberately no `hitl_confidence_threshold` float. A hardcoded
    # number is not derived from anything, and LLM self-reported confidence
    # scores 0.692 ROC AUC - worse than raw logprobs and dangerously
    # plausible-looking. The defensible construction is a precision target with
    # tau read off a risk-coverage curve on a labelled set, tiered by field
    # criticality. See clarifications.md D-3.
    target_precision_auto_accepted: float = Field(
        default=0.99,
        ge=0.0,
        le=1.0,
        description="Target precision on fields accepted without human review",
    )
    review_budget_fraction: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description=(
            "Expected fraction of field instances routed to review in year one. "
            "Falling below this means accepting lower precision - that must be an "
            "explicit decision, not an emergent one."
        ),
    )

    # --- Conflict tolerance ---
    # There is deliberately no global `numeric_conflict_tolerance` float either.
    # A 2% band on a 650 Wp nameplate is +/-13 W, which merges three adjacent
    # 5 W SKUs; the same band on a -0.29 %/degC temperature coefficient is below
    # datasheet precision. Tolerance is per-field with three kinds (exact,
    # absolute, relative) - see the table in clarifications.md D-2.

    # --- Chunking (FR-RAG-01, revised by plan.md Decision 6) ---
    # Overlap reduced from the TRS's 10-20%: systematic analysis found no
    # measurable benefit, and Docling supplies real section boundaries, which is
    # most of what overlap compensated for.
    # Bounded like every sibling: this one was the exception, and a zero or
    # negative size read from the environment is not a degraded setting but a
    # chunker that cannot terminate.
    chunk_size_tokens: int = Field(default=512, ge=1)
    chunk_overlap_ratio: float = Field(default=0.05, ge=0.0, le=0.10)

    # --- Concurrency ---
    # The ports are synchronous (plan.md Decision 10); concurrency is driven by the
    # caller via concurrent.futures and bounded here. Size the parse pool off the
    # MEAN page cost, not the median - the distribution is right-skewed, since an
    # OCR page costs roughly an order of magnitude more than a text-layer page.
    max_concurrent_parse: int = Field(
        default=4, ge=1, description="ProcessPoolExecutor width for parse/OCR"
    )
    max_concurrent_llm: int = Field(
        default=8, ge=1, description="ThreadPoolExecutor width for extraction calls"
    )
    web_search_rate_limit_per_minute: int = Field(
        default=30, ge=1, description="Cap on supplementary web queries (FR-WEB-01)"
    )

    # --- Timeouts (ADR-001 decision 3) ---
    # Every external call carries an explicit wall-clock bound; before this block
    # nothing in code or spec had one, so a hung OCR engine or an unresponsive
    # vLLM endpoint would hold a worker until the lease expired and a second worker
    # re-ran the job under it.
    #
    # **Nothing reads these yet.** No concrete adapter exists for any port, so there
    # is no call site to pass them to; the first adapter for each port must take its
    # timeout from here rather than invent one. The values are starting points like
    # every other default in this class, not spec values - D-12e fixes only the lease.
    #
    # The bounds nest: every per-call timeout <= `stage_timeout_seconds` < the job
    # lease. A stage budget shorter than one call it contains could never finish
    # that call, and a stage budget at or past the lease lets the sweeper re-queue
    # a job that is still running. Both are rejected at construction and on
    # assignment, because a violated nesting is a configuration error to raise at
    # load, not a surprise to discover in a worker at 3 a.m.
    parse_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        allow_inf_nan=False,
        description="One document through ParserPort plus OCRPort (OCR is the slow path)",
    )
    embed_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        allow_inf_nan=False,
        description="One batch through EmbedderPort or RerankerPort",
    )
    llm_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        allow_inf_nan=False,
        description="One LLMPort.extract call",
    )
    vector_store_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        allow_inf_nan=False,
        description="One VectorStorePort query or upsert",
    )
    web_search_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        allow_inf_nan=False,
        description="One supplementary web query (FR-WEB-01)",
    )
    stage_timeout_seconds: float = Field(
        default=600.0,
        gt=0,
        allow_inf_nan=False,
        description=(
            "Wall-clock budget for one WHOLE job stage, retries and sequential calls "
            "included. Must hold the slowest single call and stay strictly inside the "
            "job lease; because it bounds the stage as a whole, the lease guarantee "
            "does not depend on how many calls the stage makes."
        ),
    )

    # --- Logging (ADR-001 decision 7) ---
    log_level: LogLevel = "INFO"

    # --- Compose gate (issue #14) ---
    # Composition refuses to run while an unresolved conflict sits STRICTLY ABOVE
    # this level. MEDIUM means decision-driving specs may still be open, but a
    # pricing, warranty or certification conflict stops the workbook.
    compose_gate_threshold: Severity = Field(
        default=Severity.MEDIUM,
        le=Severity.HIGH,
        description=(
            "Unresolved conflicts strictly above this severity block composition. "
            "Capped below CRITICAL on purpose: with a strict '>', setting this to "
            "CRITICAL would disable the gate entirely, and Decision 2 requires the "
            "override to be a recorded, audited decision - an environment variable "
            "is neither."
        ),
    )

    # --- Output (FR-OUT-01) ---
    suppliers_as_rows: bool = True

    @field_validator("log_level", mode="before")
    @classmethod
    def _log_level_is_case_insensitive(cls, value: object) -> object:
        """An environment file says `info`; the stdlib's names are upper-case."""
        return value.upper() if isinstance(value, str) else value

    def __setattr__(self, name: str, value: Any) -> None:
        """Assign, and undo the assignment if the nesting check refuses it.

        Pydantic applies a `validate_assignment` write and *then* runs the model-level
        validators, so a rejected write raised while leaving the rejected value in
        place: `settings.stage_timeout_seconds = 901` raised, and the next read returned
        901. Field-level bounds such as `compose_gate_threshold`'s `le` run before the
        write and were never exposed; only a cross-field check is. A refusal that does
        not roll back is not an interlock.
        """
        missing = object()
        previous = self.__dict__.get(name, missing)
        fields_set = set(self.__pydantic_fields_set__)
        try:
            super().__setattr__(name, value)
        except Exception:
            if previous is not missing:
                self.__dict__[name] = previous
            self.__pydantic_fields_set__.clear()
            self.__pydantic_fields_set__.update(fields_set)
            raise

    @model_validator(mode="after")
    def _timeouts_nest(self) -> Settings:
        """Per-call timeouts <= the stage budget < the job lease."""
        calls = {
            "parse_timeout_seconds": self.parse_timeout_seconds,
            "embed_timeout_seconds": self.embed_timeout_seconds,
            "llm_timeout_seconds": self.llm_timeout_seconds,
            "vector_store_timeout_seconds": self.vector_store_timeout_seconds,
            "web_search_timeout_seconds": self.web_search_timeout_seconds,
        }
        slowest_name, slowest = max(calls.items(), key=lambda item: item[1])
        if self.stage_timeout_seconds < slowest:
            raise ValueError(
                f"stage_timeout_seconds ({self.stage_timeout_seconds:g}s) is shorter than "
                f"{slowest_name} ({slowest:g}s): the stage could never finish that call"
            )
        if self.stage_timeout_seconds >= JOB_LEASE_SECONDS:
            raise ValueError(
                f"stage_timeout_seconds ({self.stage_timeout_seconds:g}s) must be strictly "
                f"inside the {JOB_LEASE_SECONDS:g}s job lease (D-12e), or the lease sweeper "
                "re-queues a job that is still running"
            )
        return self


def load_settings() -> Settings:
    return Settings()
