"""#283: the malformed reply survives retry give-up. `_complete`'s give-up exit used to discard the raw reply
along with the retry loop's local state; the final reply that never validated is now written to
`.requivo/debug/` and named in the raised error."""
import anthropic
import httpx
import pytest
from _fakes import _ENGINE_REPLY, FakeClient

from requivo.providers.anthropic import run
from requivo.providers.errors import EngineError

# ── #283: the malformed reply survives retry give-up ─────────────────────────
# `_complete`'s give-up exit used to discard the raw reply along with the retry loop's local state.


class _RaisingTransportClient:
    """Raises the SDK's own transport error before any reply exists."""

    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        raise anthropic.APIConnectionError(
            message="boom", request=httpx.Request("POST", "https://api.anthropic.com"))


def test_a_retry_give_up_saves_the_final_raw_reply_and_names_it_in_the_error(tmp_path, monkeypatch):
    """The positive control: drive `_complete` all the way to give-up and check the file it leaves behind."""
    from requivo.core.errors import ProviderOutputError

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    bad_reply = '{"not": "an engine output"}'
    fake = FakeClient(bad_reply, bad_reply, bad_reply)  # every retry attempt, never conforms
    with pytest.raises(ProviderOutputError) as exc:
        run(fake, [{"role": "user", "content": "leave approval"}])

    saved = list((tmp_path / ".requivo" / "debug").glob("*.txt"))
    assert len(saved) == 1, "the give-up exit must write exactly one debug file"
    assert saved[0].read_text(encoding="utf-8") == bad_reply, (
        "the saved file must hold the exact final raw reply, byte for byte -- not a summary of it"
    )
    assert str(saved[0]) in str(exc.value), (
        "the error message must name the path a bug report should attach"
    )
    assert exc.value.details.get("raw_reply_path") == str(saved[0])


def test_a_successful_call_writes_no_debug_file(tmp_path, monkeypatch):
    """The negative half. Passes trivially if nothing ever writes the debug file at all."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    fake = FakeClient(_ENGINE_REPLY)
    run(fake, [{"role": "user", "content": "leave approval"}])
    assert not (tmp_path / ".requivo" / "debug").exists(), (
        "a successful call has nothing to debug and must write nothing"
    )


def test_a_transport_failure_writes_no_debug_file(tmp_path, monkeypatch):
    """The other negative half: an `EngineError` alone is not the trigger -- only give-up is."""
    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))
    with pytest.raises(EngineError):
        run(_RaisingTransportClient(), [{"role": "user", "content": "leave approval"}])
    assert not (tmp_path / ".requivo" / "debug").exists(), (
        "a transport-level failure has no raw reply to save and must write nothing"
    )


def test_a_prune_failure_does_not_discard_an_already_saved_reply(tmp_path, monkeypatch):
    """Found in review: `_prune_debug_dir`'s `unlink` calls carried no exception handling of their own."""
    from requivo.providers.anthropic import completion as mod

    monkeypatch.setenv("REQUIVO_WORKSPACE", str(tmp_path))

    def _raise(root):
        raise PermissionError("locked by another process")

    monkeypatch.setattr(mod, "_prune_debug_dir", _raise)
    path = mod._save_failed_reply("some raw reply text", "EngineOutput")
    assert path is not None, (
        "a write that succeeded must still be reported even when the prune right after it fails"
    )
    assert path.read_text(encoding="utf-8") == "some raw reply text"
