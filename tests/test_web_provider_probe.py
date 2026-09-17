"""The Web's provider probe has three answers, not two (#339)."""

from __future__ import annotations

import builtins

import pytest

from requivo.web.config import provider_status

_BROKEN = "cannot import name 'NotGiven' from 'httpx' (a broken transitive dependency)"


@pytest.fixture
def broken_provider_import(monkeypatch):
    """Make importing `requivo.providers.anthropic` fail for a reason that is *not* absence."""
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
    """#332 widened the `except` arm to collapse `key_present` as well as `sdk_installed`."""
    assert provider_status().key_present is None


def test_a_genuinely_absent_sdk_still_says_so_and_still_names_the_install(monkeypatch):
    """The must-fire half. `providers/anthropic/client.py` binds `Anthropic` to None when the extra is not
    installed."""
    monkeypatch.setattr("requivo.providers.anthropic.Anthropic", None)
    status = provider_status()
    assert status.sdk_installed is False
    assert status.available is False
    assert "pip install" in status.reason


def test_an_install_that_has_the_sdk_reports_it_present():
    """The third arm, and the one that keeps the other two honest."""
    assert provider_status().sdk_installed is True
