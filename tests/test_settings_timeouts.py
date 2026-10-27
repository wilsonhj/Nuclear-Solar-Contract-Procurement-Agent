"""Timeouts exist, are bounded, and nest (ADR-001 decision 3).

Before the timeout block there was no wall-clock bound anywhere in code or spec. The
property worth pinning is not that each number is positive - `test_settings_bounds.py`
already walks every numeric field for a lower bound - but that the numbers *nest*:
every per-call timeout <= the stage budget < the job lease. A violated nesting is
the case where a job is re-queued while a worker is still writing.
"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from procurement_agent.config import JOB_LEASE_SECONDS, Settings

_CALLS = (
    "parse_timeout_seconds",
    "embed_timeout_seconds",
    "llm_timeout_seconds",
    "vector_store_timeout_seconds",
    "web_search_timeout_seconds",
)
_CLARIFICATIONS = (
    Path(__file__).parent.parent / "specs" / "001-procurement-agent" / "clarifications.md"
)


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_the_lease_constant_still_matches_decision_d12e() -> None:
    """`JOB_LEASE_SECONDS` mirrors a decision that lives in prose and a DDL comment.

    Parsing D-12e keeps the outermost bound in this file from drifting if the lease is
    ever re-decided - the failure `test_severity.py` guards for the criticality table.
    """
    row = next(line for line in _CLARIFICATIONS.read_text().splitlines() if "| D-12e |" in line)
    assert "15 minutes" in row
    assert JOB_LEASE_SECONDS == 15 * 60
    # The DDL states the lease only in a comment, so this is the one check on that side.
    assert "15-minute lease" in (_CLARIFICATIONS.parents[2] / "sql" / "08_job.sql").read_text()


def test_the_defaults_nest_and_are_accepted() -> None:
    settings = _settings()
    assert max(getattr(settings, name) for name in _CALLS) <= settings.stage_timeout_seconds
    assert settings.stage_timeout_seconds < JOB_LEASE_SECONDS


@pytest.mark.parametrize("name", [*_CALLS, "stage_timeout_seconds"])
@pytest.mark.parametrize("bad", [0, -1.0, float("inf"), float("nan")])
def test_a_non_positive_or_non_finite_timeout_is_refused(name: str, bad: float) -> None:
    """Zero is "never wait", infinity is "never stop", NaN compares false to everything."""
    with pytest.raises(ValidationError):
        _settings(**{name: bad})


@pytest.mark.parametrize("name", _CALLS)
def test_a_call_timeout_longer_than_the_stage_is_refused(name: str) -> None:
    """The stage could never finish the call it is meant to contain."""
    with pytest.raises(ValidationError, match=name):
        _settings(**{name: 601.0})  # default stage budget is 600s


def test_a_call_timeout_equal_to_the_stage_is_accepted() -> None:
    """The boundary is inclusive: the stage may be exactly one call long."""
    assert _settings(llm_timeout_seconds=600.0).llm_timeout_seconds == 600.0


def test_a_stage_budget_at_or_past_the_lease_is_refused() -> None:
    """Strictly inside: at the lease the sweeper and the worker race."""
    with pytest.raises(ValidationError, match="job lease"):
        _settings(stage_timeout_seconds=JOB_LEASE_SECONDS)
    just_inside = _settings(stage_timeout_seconds=JOB_LEASE_SECONDS - 1)
    assert just_inside.stage_timeout_seconds == JOB_LEASE_SECONDS - 1


def test_the_nesting_holds_under_assignment() -> None:
    """`validate_assignment=True` is what makes a bound mean something after construction."""
    settings = _settings()
    with pytest.raises(ValidationError):
        settings.stage_timeout_seconds = settings.llm_timeout_seconds - 1
    with pytest.raises(ValidationError):
        settings.llm_timeout_seconds = settings.stage_timeout_seconds + 1


def test_a_refused_assignment_leaves_the_old_value_in_place() -> None:
    """A rejected write must not half-apply: the interlock is only as good as its rollback."""
    settings = _settings()
    before = settings.stage_timeout_seconds
    with pytest.raises(ValidationError):
        settings.stage_timeout_seconds = JOB_LEASE_SECONDS + 1
    assert settings.stage_timeout_seconds == before


def test_the_log_level_accepts_an_environment_files_spelling() -> None:
    assert _settings(log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        _settings(log_level="verbose")
