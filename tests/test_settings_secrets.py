"""Credential-shaped settings do not render their own plaintext.

**This pins a property, not a fix for an observed leak.** Nothing in `src/` reads
these fields today and `Settings` has no export path - no endpoint serves it, no
audit payload embeds it. The gap is that nothing *stops* a value reaching a log
line, an exception message or a hashed payload if one ever does: a plain `str`
field renders itself in `repr()`, in `str()`, in an f-string and in both dump
forms, and every one of those is a place a credential can arrive without anyone
choosing to put it there.

The five fields are not listed here. They are matched by name, so a
`redis_url` or `stripe_api_key` added later arrives protected rather than
arriving plain and waiting for someone to notice - the failure mode
`services/confidence` already shipped, where `ul_listing` fell out of a
hand-written list and auto-accepted at 0.99.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from procurement_agent.config import Settings

#: A value that cannot occur by accident, so a leak assertion cannot pass because
#: the field happened to be empty.
SENTINEL = "PLAINTEXT-THAT-MUST-NEVER-RENDER-8f3a1c"

#: What "credential-shaped" means, as a rule rather than a roster. `_api_key` and
#: `_url` are the two shapes the contract surface actually has: a bearer token,
#: and a DSN whose userinfo carries a password. Deliberately does not match
#: `llm_endpoint` or `embedding_model`, which name a service rather than
#: authorise anything.
_CREDENTIAL_PATTERN = re.compile(r"(_api_key|_url|_secret|_token|_password|_dsn)$")

_CREDENTIAL_FIELDS = sorted(
    name for name in Settings.model_fields if _CREDENTIAL_PATTERN.search(name)
)


def _isolated(**overrides: Any) -> Settings:
    """`Settings` built without reading the ambient environment or a local `.env`.

    Same helper as `test_settings_bounds.py`, and for the same reason: a bare
    `Settings()` reads both, and this branch ships a `.env.example`.
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_the_credential_fields_are_discovered() -> None:
    """Guards the parametrisation below.

    An empty or shrunken list would make every leak case vanish while the suite
    stayed green - the same vacuity `test_the_numeric_settings_are_discovered`
    exists to prevent one file over.
    """
    assert _CREDENTIAL_FIELDS == [
        "database_url",
        "llm_api_key",
        "object_store_url",
        "vector_store_url",
        "web_search_api_key",
    ]


@pytest.mark.parametrize("name", _CREDENTIAL_FIELDS)
def test_every_credential_shaped_field_is_a_secret(name: str) -> None:
    """The rule itself, so a new credential-shaped field cannot be added plain.

    Checked on the declared annotation rather than on a constructed value: a
    field is protected by how it is declared, and a test that only inspected an
    instance would pass for a field nobody happened to populate.
    """
    annotation = Settings.model_fields[name].annotation
    assert annotation is not None
    assert SecretStr in getattr(annotation, "__args__", (annotation,)), (
        f"{name} is credential-shaped but declared {annotation}; it must be SecretStr"
    )


@pytest.mark.parametrize("name", _CREDENTIAL_FIELDS)
def test_a_credential_does_not_leak_through_repr(name: str) -> None:
    """`repr()` is what a debugger, a REPL and most traceback frames print."""
    assert SENTINEL not in repr(_isolated(**{name: SENTINEL}))


@pytest.mark.parametrize("name", _CREDENTIAL_FIELDS)
def test_a_credential_does_not_leak_through_str(name: str) -> None:
    """`str()` is what `logging.info("%s", settings)` writes."""
    assert SENTINEL not in str(_isolated(**{name: SENTINEL}))


@pytest.mark.parametrize("name", _CREDENTIAL_FIELDS)
def test_a_credential_does_not_leak_through_an_fstring(name: str) -> None:
    """The field interpolated on its own, which is how it would reach an error
    message written by someone who never saw this file."""
    settings = _isolated(**{name: SENTINEL})
    assert SENTINEL not in f"{getattr(settings, name)}"


@pytest.mark.parametrize("name", _CREDENTIAL_FIELDS)
def test_a_credential_does_not_leak_through_model_dump(name: str) -> None:
    """`model_dump()` keeps the `SecretStr` wrapper rather than unwrapping it.

    Worth asserting rather than assuming, because the two dump modes differ and
    the wrong guess is silent: `mode="json"` renders the mask as a string, while
    plain `model_dump()` returns the object, whose own `repr` is the mask. Both
    are safe; neither is safe *by default* for a plain `str` field.
    """
    dumped = _isolated(**{name: SENTINEL}).model_dump()
    assert SENTINEL not in str(dumped)
    assert SENTINEL not in str(_isolated(**{name: SENTINEL}).model_dump(mode="json"))


@pytest.mark.parametrize("name", _CREDENTIAL_FIELDS)
def test_a_credential_does_not_leak_through_model_dump_json(name: str) -> None:
    """The path an audit payload or a config snapshot would actually take."""
    assert SENTINEL not in _isolated(**{name: SENTINEL}).model_dump_json()


@pytest.mark.parametrize("name", _CREDENTIAL_FIELDS)
def test_serialising_a_dump_with_json_refuses_rather_than_leaking(name: str) -> None:
    """`json.dumps(settings.model_dump())` raises instead of writing a secret.

    A caller reaching for the obvious two-step - dump, then serialise - gets a
    `TypeError` naming `SecretStr` rather than a JSON document with a live
    credential in it. Failing loudly is the behaviour this repo asks for
    everywhere else it had the choice (`encode_value`'s closed world,
    `require_contract_key`), and it is pinned here so a future `default=str`
    somewhere cannot quietly turn a refusal back into a leak.
    """
    dumped = _isolated(**{name: SENTINEL}).model_dump()
    with pytest.raises(TypeError, match="SecretStr"):
        json.dumps(dumped)


@pytest.mark.parametrize("name", _CREDENTIAL_FIELDS)
def test_a_credential_loads_from_the_environment_and_is_still_readable(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Masking must not cost the value.

    `Settings` is env-driven under `PROCUREMENT_`, so the round trip that matters
    is variable in, plaintext out - via `get_secret_value()`, which is greppable
    and therefore reviewable in a way bare attribute access is not.
    """
    monkeypatch.setenv(f"PROCUREMENT_{name.upper()}", SENTINEL)
    loaded = getattr(Settings(_env_file=None), name)  # type: ignore[call-arg]
    assert loaded.get_secret_value() == SENTINEL


def test_a_credential_loads_from_a_dotenv_file(tmp_path: Path) -> None:
    """The other supported source, which `model_config` names explicitly.

    Checked separately from the environment case because `.env` parsing is
    pydantic-settings' own code path, not `os.environ`, and a type it could not
    construct would fail here first.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(f"PROCUREMENT_LLM_API_KEY={SENTINEL}\n")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == SENTINEL
    assert SENTINEL not in repr(settings)


@pytest.mark.parametrize("name", _CREDENTIAL_FIELDS)
def test_assigning_a_credential_after_construction_still_masks_it(name: str) -> None:
    """`validate_assignment=True` is what makes this hold on the assignment path.

    The interlock is already in `model_config` for the compose-gate bound, and it
    earns its keep twice here: without it a later `settings.llm_api_key = "..."`
    would store a bare `str` on a field the type says is a `SecretStr`, and the
    masking would be true only of values that arrived through the constructor.
    """
    settings = _isolated()
    setattr(settings, name, SENTINEL)
    assert SENTINEL not in repr(settings)
    assert getattr(settings, name).get_secret_value() == SENTINEL


def test_a_non_credential_setting_is_left_readable() -> None:
    """The rule is scoped, not blanket.

    `llm_endpoint` names a service and belongs in a log line - masking it would
    cost debuggability and buy nothing, since a URL with no userinfo authorises
    nobody. This fails if the pattern above is ever widened carelessly.
    """
    settings = _isolated(llm_endpoint="https://vllm.internal:8000/v1")
    assert "vllm.internal" in repr(settings)


def test_endpoint_settings_refuse_embedded_userinfo_credentials() -> None:
    """SEC-MEDIUM-2: `llm_endpoint` and `embedding_endpoint` are plain `str`
    fields because 'a URL with no userinfo authorises nobody'. Supplying
    `https://user:password@host/v1` in an endpoint URL must be rejected at
    construction and assignment so credentials cannot bypass `SecretStr`."""
    with pytest.raises(ValueError, match="userinfo"):
        _isolated(llm_endpoint="https://admin:secret@vllm.internal:8000/v1")
    with pytest.raises(ValueError, match="userinfo"):
        _isolated(embedding_endpoint="https://token@embed.internal:8000/v1")
