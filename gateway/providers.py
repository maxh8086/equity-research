"""Provider backends. The only modules in the repo that may import a vendor SDK.

Each provider takes a fully-built prompt and a JSON schema and returns raw
JSON text plus the *resolved* model id. The resolved id matters: an alias like
`claude-sonnet-5` can point at different weights over time, and
`model_version` in the stores has to say which weights produced a row (R2).

The SDK import is deliberately inside the method. The repo does not depend on
any provider SDK, so importing at module scope would make `import gateway`
fail everywhere -- including in the architecture tests, which must be able to
read these files without the SDK installed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderUnavailable(RuntimeError):
    """The provider cannot be reached: SDK missing, no credentials, or a transport error."""


class Provider(Protocol):
    """A model backend.

    `name` identifies it in `extracted_by`. `generate` returns the raw JSON
    text and the resolved model id, and raises `ProviderUnavailable` rather
    than leaking a vendor exception type past the gateway.
    """

    name: str

    def generate(
        self,
        *,
        system: str,
        prompt: str,
        json_schema: dict[str, Any],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, str]: ...


@dataclass
class FakeProvider:
    """A scripted provider for tests. Never reaches a network.

    `responses` are returned in order, each already JSON text. Prompts are
    recorded so a test can assert what was actually sent -- that the untrusted
    guard was present, that the document was fenced, that the schema went with
    it.
    """

    responses: list[str] = field(default_factory=list)
    resolved_model: str = "fake-model-2026-01-01"
    name: str = "fake"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self,
        *,
        system: str,
        prompt: str,
        json_schema: dict[str, Any],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, str]:
        self.calls.append(
            {
                "system": system,
                "prompt": prompt,
                "json_schema": json_schema,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if not self.responses:
            raise ProviderUnavailable("FakeProvider ran out of scripted responses")
        return self.responses.pop(0), self.resolved_model


@dataclass
class AnthropicProvider:
    """Anthropic Messages API, using a tool call to force the output schema.

    A tool with the schema as its input is how the API is made to return an
    object that validates, rather than prose containing JSON. CLAUDE.md
    requires a Pydantic schema for every LLM output and no free-text
    responses; this is where that is enforced on the wire.
    """

    api_key: str | None = None
    name: str = "anthropic"
    _tool_name: str = "record"

    def generate(
        self,
        *,
        system: str,
        prompt: str,
        json_schema: dict[str, Any],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, str]:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ProviderUnavailable(
                "the anthropic SDK is not installed; add it to requirements.txt "
                "(and regenerate the pinned file) before using this provider"
            ) from exc

        try:
            client = anthropic.Anthropic(api_key=self.api_key) if self.api_key else anthropic.Anthropic()
            message = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=[
                    {
                        "name": self._tool_name,
                        "description": "Record the extracted result.",
                        "input_schema": json_schema,
                    }
                ],
                tool_choice={"type": "tool", "name": self._tool_name},
            )
        except Exception as exc:  # pragma: no cover - network path
            raise ProviderUnavailable(f"anthropic request failed: {exc}") from exc

        for block in message.content:
            if getattr(block, "type", None) == "tool_use":
                return json.dumps(block.input), message.model
        raise ProviderUnavailable(  # pragma: no cover - the API was told tool_choice
            "the response carried no tool_use block, so no schema-shaped output"
        )
