"""The Web's provider probe has three answers, not two (#339).

`provider_status()` wrapped its whole probe in `except Exception: sdk = False`, so *any* import
failure of `requivo.providers.anthropic` -- a broken transitive dependency, a partially installed
package, an incompatible SDK major, a syntax error in an installed module -- rendered as
`sdk_installed=False`, which the UI states as "Install the provider: pip install
\'requivo[anthropic]\'". A reader who has already done exactly that is told to do it again, and the
real cause is never named. That is this repository\'s own defect class, one layer out: an absence
produced by the tool -- *we could not look* -- read as an absence in the world.

The pair below is the point. Asserting only the broken-import case would pass against code that had
simply stopped claiming anything, and asserting only the genuine absence would pass against the
collapse this file exists to catch.

Deliberately not in `tests/web/test_config.py`, where #332\'s sibling assertions live: that file was
held by another change in flight when this landed. Its subject is the same probe.
"""

from __future__ import annotations

import builtins

import pytest

from requivo.web.config import provider_status

_BROKEN = "cannot import name 'NotGiven' from 'httpx' (a broken transitive dependency)"


@pytest.fixture
def broken_provider_import(monkeypatch):
    """Make importing `requivo.providers.anthropic` fail for a reason that is *not* absence.

    Patching `__import__` rather than deleting the module from `sys.modules`: the probe\'s import is
    function-local and would otherwise be served from the module cache, and a `None` entry raises an
    ImportError whose message is about `sys.modules` rather than about anything a reader would
    recognise. The message here is one a real broken install produces, so the assertion that it
    reaches the surface is an assertion about something worth surfacing.
    """
    real = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "requivo.providers.anthropic":
            raise ImportError(_BROKEN)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)


def test_an_import_that_failed_for_another_reason_is_not_reported_as_not_installed(
        broken_provider_import):
    status = provider_status()
    assert status.sdk_installed is None, (
        "an import failure that is not absence must not answer the absence question at all")
    assert status.available is False
    assert "pip install" not in status.reason, (
        "the reader has already installed it; prescribing the install again is the whole defect")
    assert _BROKEN in status.reason, "the actual cause has to reach the reader"
    assert "ImportError" in status.reason


def test_the_key_half_is_not_claimed_either_when_the_probe_could_not_look(broken_provider_import):
    """#332 widened the `except` arm to collapse `key_present` as well as `sdk_installed`. It is not
    user-visible today -- `reason` gates on the SDK first -- so it is asserted here rather than left
    to be discovered when some later branch does read it."""
    assert provider_status().key_present is None


def test_a_genuinely_absent_sdk_still_says_so_and_still_names_the_install(monkeypatch):
    """The must-fire half. `providers/anthropic/client.py` binds `Anthropic` to None when the extra
    is not installed -- the import *succeeds* and the handle is absent, which is a fact the probe
    really did establish."""
    monkeypatch.setattr("requivo.providers.anthropic.Anthropic", None)
    status = provider_status()
    assert status.sdk_installed is False
    assert status.available is False
    assert "pip install" in status.reason


def test_an_install_that_has_the_sdk_reports_it_present():
    """The third arm, and the one that keeps the other two honest: a probe stuck on `None` would
    satisfy the broken-import test, and a probe stuck on `False` would satisfy the absence test.
    The dev/CI environment installs the `anthropic` extra, so this is the observable ground truth."""
    assert provider_status().sdk_installed is True
