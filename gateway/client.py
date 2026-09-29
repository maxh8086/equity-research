"""The one call every model request goes through.

`complete()` takes a Pydantic model class and returns an instance of it, or
raises. There is no path that returns free text (CLAUDE.md: "no free-text
model responses"), and no path that reaches a provider without the untrusted
document guard when a document is supplied.

What comes back carries its own provenance: the resolved model id and a hash
of the exact prompt, so a stored row can name the weights and the input that
produced it, and a prompt change is visible as a different hash rather than
as silently different output.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from gateway.providers import Provider, ProviderUnavailable
from gateway.untrusted import wrap_untrusted

T = TypeVar("T", bound=BaseModel)


class GatewayError(RuntimeError):
    """The request could not be completed."""


class SchemaRejected(GatewayError):
    """The model returned something that is not an instance of the requested schema.

    Never repaired here. A model that cannot fill the schema is a finding --
    about the prompt, the document, or the model tier -- and the caller
    quarantines the document rather than accepting a coerced row.
    """


class ModelTier(StrEnum):
    """Named tiers, so a caller states intent rather than pinning a string.

    Guidance extraction runs on SONNET and the plan says not to downgrade it:
    hedge strength is a judgement about wording ("we will" vs "we are working
    towards"), and a cheaper tier flattens exactly that distinction. The
    extraction-accuracy test is what would catch a downgrade, and CLAUDE.md
    "Testing" says a failing validation reverts the tier rather than lowering
    the threshold.
    """

    SONNET = "claude-sonnet-5"
    OPUS = "claude-opus-5"
    HAIKU = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class Completion[T: BaseModel]:
    """A validated model output and the provenance of the call that produced it."""

    value: T
    model_version: str
    extracted_by: str
    prompt_hash: str


def _schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """A JSON schema the provider will accept.

    `additionalProperties: false` at the top level so a model cannot smuggle
    an extra field past a schema that would otherwise ignore it.
    """
    schema = model.model_json_schema()
    schema.setdefault("additionalProperties", False)
    return schema


def complete(
    *,
    provider: Provider,
    schema: type[T],
    system: str,
    instructions: str,
    document: str | None = None,
    model: ModelTier | str = ModelTier.SONNET,
    max_tokens: int = 8192,
    temperature: float = 0.0,
) -> Completion[T]:
    """Run one model call and return `schema` validated, or raise.

    `instructions` is trusted text this repo wrote. `document` is not: if it
    is given it is fenced and prefixed with the untrusted-input guard, and it
    always comes after the instructions, so the trusted part is never
    displaced by a long document.

    `temperature` defaults to 0: extraction should be as reproducible as the
    provider allows, and the prompt hash is only useful next to a
    deterministic setting.
    """
    if not instructions.strip():
        raise GatewayError("instructions are empty; a document alone is not a prompt")

    prompt = instructions if document is None else f"{instructions}\n\n{wrap_untrusted(document)}"
    model_id = model.value if isinstance(model, ModelTier) else model
    json_schema = _schema_for(schema)

    prompt_hash = hashlib.sha256(
        json.dumps(
            {"system": system, "prompt": prompt, "schema": json_schema, "model": model_id},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    try:
        raw, resolved_model = provider.generate(
            system=system,
            prompt=prompt,
            json_schema=json_schema,
            model=model_id,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except ProviderUnavailable:
        raise
    except Exception as exc:  # pragma: no cover - a provider that breaks its contract
        raise GatewayError(f"provider {provider.name!r} failed: {exc}") from exc

    try:
        value = schema.model_validate_json(raw)
    except ValidationError as exc:
        raise SchemaRejected(
            f"{provider.name} returned output that is not a valid {schema.__name__}: {exc}"
        ) from exc

    return Completion(
        value=value,
        model_version=resolved_model,
        extracted_by=f"{provider.name}:{model_id}",
        prompt_hash=prompt_hash,
    )
