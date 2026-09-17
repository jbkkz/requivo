"""Pins the SDK `Usage` field names `_complete` reads through `getattr(u, name, 0) or 0` (#294)."""
import ast
import inspect

import pytest

anthropic = pytest.importorskip("anthropic", reason="requires the 'anthropic' extra")

from anthropic.types import Usage  # noqa: E402 - after importorskip, by construction

from requivo.providers.anthropic import completion as _completion_module  # noqa: E402


def _usage_field_names_completion_reads() -> tuple[str, ...]:
    """Every string literal `name` in a `getattr(u, name, 0)` call inside `completion.py`."""
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
    """A sanity control on the extractor itself, not on the SDK: if the AST walk's shape assumption breaks."""
    names = _usage_field_names_completion_reads()
    assert names, (
        'found no getattr(u, "...", 0) calls in completion.py -- either the billing read site '
        "no longer reads usage that way (update the extractor above to match), or it moved out of "
        "this module entirely."
    )


def test_the_sdk_usage_object_still_has_every_field_completion_py_reads():
    """Red the moment a name `completion.py` actually reads is missing from the installed SDK."""
    names = _usage_field_names_completion_reads()
    present = set(Usage.model_fields)
    missing = [name for name in names if name not in present]
    assert not missing, (
        f"anthropic.types.Usage no longer defines {missing} -- completion.py's _complete() reads "
        "these through getattr(u, name, 0) or 0, so a rename here currently zeroes that field's "
        "billing silently, with nothing going red. Update the read site in "
        "src/requivo/providers/anthropic/completion.py to the new name(s)."
    )
