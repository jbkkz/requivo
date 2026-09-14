"""Pins the SDK `Usage` field names `_complete` reads through `getattr(u, name, 0) or 0` (#294).

`completion.py` reads `input_tokens`, `output_tokens`, `cache_read_input_tokens` and
`cache_creation_input_tokens` off the SDK's response with a bare `getattr` default of `0` --
deliberately, so the offline fake in this suite needs no real `Usage` object (see
`test_a_failed_call_is_still_recorded_on_every_exit` in `test_usage.py`, which relies on exactly
that tolerance). The cost of that tolerance is silent: if a future `anthropic` release, still inside
the declared `anthropic` extra's version range, renames or removes one of these attributes, every
token count for that field quietly becomes 0 forever -- the ledger under-reports real spend,
`requivo doctor` and every cost estimate downstream go on trusting a zero that is not a zero, and
nothing anywhere goes red. That is the "silent absence inside the check for it" shape this repository
names as its enemy throughout CLAUDE.md. This file does not change the `getattr` tolerance -- the
fake-client ergonomics it buys are fine -- it only makes a rename of one of these names loud instead
of quiet. (The declared floor was `anthropic>=0.40.0,<2` when this file was first written for #294;
writing this very test found that two of the four names were genuinely absent from the SDK's `Usage`
class before 0.42.0, a live gap rather than a hypothetical future rename -- see the `pyproject.toml`
comment above the `anthropic` extra, which is now `anthropic>=0.42.0,<2`. This file names no version
number itself, for exactly the reason that reference just went stale: the floor is `pyproject.toml`'s
fact to state, not this file's to restate and then fall behind.)

Two failure modes, one test. The field names asserted below are read directly out of
`completion.py`'s own source (its AST, not a hand-copied literal), so this file cannot itself drift
from the call site it pins -- a name added, removed or typo'd at the read site changes what gets
checked, automatically, with no second edit required here. What stays fixed is the question asked of
each one: is it still on `anthropic.types.Usage`? That is the class #294 was filed for, and pinning a
hand-copied literal would only have caught it if this file happened to be updated in the same change
that touched the read site -- which is exactly the kind of accidental agreement a rename could slip
past.

Skips cleanly (not an error) when the `anthropic` extra is not installed: `pytest.importorskip` at
module scope means an install without `requivo[anthropic]` reports this file as skipped rather than
failing to collect it, so it does not cost the rest of the suite.
"""
import ast
import inspect

import pytest

anthropic = pytest.importorskip("anthropic", reason="requires the 'anthropic' extra")

from anthropic.types import Usage  # noqa: E402 - after importorskip, by construction

from requivo.providers.anthropic import completion as _completion_module  # noqa: E402


def _usage_field_names_completion_reads() -> tuple[str, ...]:
    """Every string literal `name` in a `getattr(u, name, 0)` call inside `completion.py`, read via
    that module's own AST -- see the module docstring for why this is a source read rather than a
    retyped list. `inspect.getsource` on the already-imported module, not a re-read of the file by
    path, so this walks exactly the code that actually runs in this process."""
    tree = ast.parse(inspect.getsource(_completion_module))
    names = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name) and node.args[0].id == "u"
            and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
        ):
            names.append(node.args[1].value)
    return tuple(names)


def test_the_extractor_still_finds_getattr_u_calls_in_completion_py():
    """A sanity control on the extractor itself, not on the SDK: if the AST walk's shape
    assumption breaks -- a different accessor, a helper, a loop over a tuple -- it would quietly
    find nothing and the test below would pass vacuously, the exact silent-absence shape this
    file exists to avoid. This is the third-state check for the extractor itself, so a shape
    change is loud before a name change ever needs to be.
    """
    names = _usage_field_names_completion_reads()
    assert names, (
        'found no getattr(u, "...", 0) calls in completion.py -- either the billing read site '
        "no longer reads usage that way (update the extractor above to match), or it moved out of "
        "this module entirely."
    )


def test_the_sdk_usage_object_still_has_every_field_completion_py_reads():
    """Red the moment a name `completion.py` actually reads is missing from the installed SDK.

    `getattr(u, name, 0) or 0` never raises on a missing attribute, so a rename upstream would
    otherwise surface as nothing at all -- not an exception, just every token count for that
    field silently pinned to 0. This assertion turns that silence into a failing test instead.
    """
    names = _usage_field_names_completion_reads()
    present = set(Usage.model_fields)
    missing = [name for name in names if name not in present]
    assert not missing, (
        f"anthropic.types.Usage no longer defines {missing} -- completion.py's _complete() reads "
        "these through getattr(u, name, 0) or 0, so a rename here currently zeroes that field's "
        "billing silently, with nothing going red. Update the read site in "
        "src/requivo/providers/anthropic/completion.py to the new name(s)."
    )
