"""The LLM gateway: the only code in this repo that may import a provider SDK.

A top-level package outside `core/` on purpose (CLAUDE.md "Code conventions"),
so the dependency arrow points one way: `extract/` and `narrate/` import
`gateway`, and nothing else does. `tests/test_architecture.py` rules 1 and 6
enforce both halves.

Everything a caller needs is re-exported here.
"""

from gateway.client import (
    Completion,
    GatewayError,
    ModelTier,
    SchemaRejected,
    complete,
)
from gateway.providers import FakeProvider, Provider, ProviderUnavailable
from gateway.untrusted import UNTRUSTED_DOCUMENT_GUARD, wrap_untrusted

__all__ = [
    "UNTRUSTED_DOCUMENT_GUARD",
    "Completion",
    "FakeProvider",
    "GatewayError",
    "ModelTier",
    "Provider",
    "ProviderUnavailable",
    "SchemaRejected",
    "complete",
    "wrap_untrusted",
]
