"""The Anthropic reasoning provider, the only package that calls the Anthropic API. `anthropic` is
an optional dependency (`requivo[anthropic]`), refused cleanly at `new_client()`. Split by cohesion
(#74): `client.py`, `pricing.py`, `completion.py`, `generators.py`, `provider.py`. This module
re-exports the public surface (`requivo.providers` is not a stable API, `docs/compatibility.md`);
the underscore names are imported from the module they live in.
"""

from __future__ import annotations

from requivo.providers.anthropic.client import (
    MODEL_DEFAULT,
    Anthropic,
    APIError,
    credential_diagnosis,
    credential_present,
    current_model_name,
    new_client,
)
from requivo.providers.anthropic.completion import MAX_OUTPUT_TOKENS
from requivo.providers.anthropic.generators import (
    advise,
    answer_turn,
    derive_stories,
    estimate,
    generate_criteria,
    generate_epic,
    generate_prd,
    generate_release,
    prompt_version,
    run,
)
from requivo.providers.anthropic.pricing import PRICING_AS_OF, price_call, price_per_mtok
from requivo.providers.anthropic.provider import AnthropicProvider

# `EngineError`, `UsageLedger`, `CallRecord` and `track_usage` are not re-exported (#167): they are
# `requivo.usage` and `requivo.providers.errors`, and a renderer must not reach them through a vendor.
__all__ = [
    "MAX_OUTPUT_TOKENS",
    "MODEL_DEFAULT",
    "PRICING_AS_OF",
    "APIError",
    "Anthropic",
    "AnthropicProvider",
    "advise",
    "answer_turn",
    "credential_diagnosis",
    "credential_present",
    "current_model_name",
    "derive_stories",
    "estimate",
    "generate_criteria",
    "generate_epic",
    "generate_prd",
    "generate_release",
    "new_client",
    "price_call",
    "price_per_mtok",
    "prompt_version",
    "run",
]
