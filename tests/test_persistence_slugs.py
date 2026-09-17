"""Slug derivation and validation, and the model loader's corruption handling (#72, #555)."""
from __future__ import annotations

import json

import pytest

from conftest import full_model as _full_model
from requivo.core import persistence as store
from requivo.core.errors import ModelUnreadableError, RequivoError, SessionNotFoundError
from requivo.core.persistence import derive_slug, load_model
from requivo.services.sessions import SessionService

# ── the slug derive_slug() emits, and what validate_slug()/validate_filename() refuse ───


def test_slug_is_first_five_word_tokens():
    # The five-token rule survives #245; what changed is *which* five.
    assert derive_slug("We'd like an invoice created automatically when signed") == (
        "invoice-created-automatically-signed")
    assert derive_slug("!!!") == "discovery"


def test_a_slug_carries_content_words_rather_than_the_request_opening():
    """#245. The slug is the handle a user retypes into `answer`, `status`, `brief` and `prd`, so a handle
    built from the phrase every request opens with is both unmemorable and collision-prone."""
    assert derive_slug("We need a way to track vendor invoices.") == "track-vendor-invoices"
    assert derive_slug("We need a leave approval system.") == "leave-approval-system"
    # Two requests that used to share the whole slug now describe themselves.
    assert derive_slug("We need a way to track vendor invoices") != derive_slug(
        "We need a way to archive old contracts")


def test_the_stopword_list_keeps_the_words_its_own_comment_promises_to_keep():
    """#245, and the guard the comment needed rather than a second copy of it."""
    from requivo.core.persistence import _SLUG_STOPWORDS

    for word in ("son", "hay", "sin", "man", "war", "bin", "hat"):
        assert word not in _SLUG_STOPWORDS, (
            f"{word!r} is an ordinary English content word and the comment above _SLUG_STOPWORDS "
            "says it was deliberately excluded")
    # Must fire: the list is the real one and is not empty, so the loop above is a real check.
    assert {"the", "nous", "der", "para"} <= _SLUG_STOPWORDS
    assert "son" in derive_slug("Track the son of the account owner").split("-")


def test_a_slug_folds_diacritics_rather_than_splitting_the_word():
    """#245. `[a-z0-9]+` treats an accented letter as a separator, so it does not merely drop the accent."""
    fr = derive_slug("Nous aimerions un système d'approbation des congés payés").split("-")
    assert "systeme" in fr and "conges" in fr
    assert "syst" not in fr and "me" not in fr

    assert derive_slug("Podríamos automatizar la aprobación de vacaciones").split("-") == [
        "automatizar", "aprobacion", "vacaciones"]
    assert derive_slug("Ein Genehmigungssystem für Urlaubsanträge").split("-") == [
        "genehmigungssystem", "urlaubsantrage"]


def test_folding_expands_a_latin_letter_that_carries_no_combining_mark():
    """#245. NFKD decomposes a letter into base + mark and the ASCII fold then drops the mark."""
    assert "strassenverkehr" in derive_slug("Straßenverkehr melden").split("-")
    assert "oekosystem" in derive_slug("Œkosystem pflegen").split("-")


def test_a_request_of_nothing_but_stopwords_still_derives_a_usable_slug():
    """#245. Filtering can empty the token list, and an empty list means the `discovery` fallback."""
    assert derive_slug("We need it") == "we-need-it"
    assert derive_slug("We need a way to") == "we-need-a-way-to"
    from requivo.core.persistence import validate_slug
    validate_slug(derive_slug("We need it"))


def test_a_non_latin_request_still_derives_the_documented_discovery_fallback():
    """#245, and the residual limit stated rather than fixed."""
    assert derive_slug("休暇承認システムが必要です") == "discovery"
    assert derive_slug("Нам нужна система одобрения отпусков") == "discovery"


def test_invalid_slug_is_rejected_before_touching_the_filesystem():
    # The traversal guard: an explicit slug that could escape the session root must raise in Core.
    from requivo.core.errors import InvalidSlugError
    from requivo.core.persistence import canonical_dir, validate_slug
    for bad in ("../../escaped", "a/b", "..", ".", "", "/abs", "Upper", "under_score"):
        with pytest.raises(InvalidSlugError):
            validate_slug(bad)
        with pytest.raises(InvalidSlugError):
            canonical_dir(bad)
    assert validate_slug("leave-approval") == "leave-approval"   # the shape derive_slug() always emits


def test_reserved_windows_device_names_are_refused_as_slugs():
    # #221: con/nul/aux/prn/com1-9/lpt1-9 cannot be created as files or directories on Windows.
    from requivo.core.errors import InvalidSlugError
    from requivo.core.persistence import validate_slug
    reserved = ("con", "prn", "aux", "nul",
                "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
                "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9")
    for name in reserved:
        with pytest.raises(InvalidSlugError):
            validate_slug(name)
        with pytest.raises(InvalidSlugError):
            validate_slug(name.upper())
    # Must-not-fire control: names that merely resemble the reserved set stay valid.
    for ok in ("console", "com0", "lpt", "con-approval", "prnter"):
        assert validate_slug(ok) == ok


def test_creating_a_reserved_slug_is_still_refused_through_canonical_dir_directly(workspace):
    # #372: `create_session` calls `canonical_dir`, never `validate_slug` directly, so the previous test (which only exercises `validate_slug`) does not actually pin what stops a *new* 'con' session from being created.
    from requivo.core.errors import InvalidSlugError
    with pytest.raises(InvalidSlugError):
        store.canonical_dir("con")
    with pytest.raises(InvalidSlugError):
        store.create_session("con", "A request that would slug to a reserved name.")
    with pytest.raises(InvalidSlugError):
        with store.session_lock("nul"):
            pass  # pragma: no cover - refused before the body ever runs


@pytest.mark.skipif(store.fcntl is None, reason="the fixture cannot be built on this platform: "
                     "Windows refuses to create a directory literally named 'con' at the OS level, "
                     "independent of anything Requivo's own code does (see the module comment above "
                     "_RESERVED_DEVICE_NAMES) -- so a session already on disk under a reserved name "
                     "is a state only a platform that never enforced the restriction can reach. "
                     "REASONED, NOT OBSERVED: no Windows machine confirmed this by hand; it follows "
                     "from the documented Windows behaviour #221 already relies on.")
def test_a_session_already_on_disk_under_a_reserved_slug_is_readable_by_every_verb_that_named_it(
        workspace):
    # #372: a session already on disk under a Windows reserved name.
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("A request captured before #221 shipped.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "deadbeef", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")

    # Must-fire: every read path this issue named tolerates the existing directory.
    assert store.session_exists("con") is True
    assert store.canonical_dir("con") == d
    assert store.read_meta("con").slug == "con"
    assert store.session_request("con") == "A request captured before #221 shipped."
    assert "con" in store.list_session_slugs()
    # `session export`'s own read-consistency lock.
    with store.session_lock("con"):
        pass

    # Must-not-fire control, in the same fixture (a negative needs a positive beside it, #221).
    from requivo.core.errors import InvalidSlugError
    with pytest.raises(InvalidSlugError):
        store.canonical_dir("nul")
    with pytest.raises(InvalidSlugError):
        with store.session_lock("nul"):
            pass  # pragma: no cover - refused before the body ever runs


@pytest.mark.skipif(store.fcntl is None, reason="same platform limit as the sibling test above: "
                     "the fixture needs a directory literally named 'con' already on disk, which "
                     "Windows itself refuses to create. REASONED, NOT OBSERVED.")
def test_idempotent_reinit_of_an_existing_reserved_slug_returns_it_rather_than_creating_one(
        workspace):
    # #372, the corollary that validates the read/creation split is drawn in the right place.
    d = store.session_root() / "con"
    (d / "revisions").mkdir(parents=True)
    (d / "artifacts").mkdir()
    (d / "request.md").write_text("Some request.", encoding="utf-8")
    (d / "session.json").write_text(json.dumps({
        "session_id": "original-id", "slug": "con", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "provider": None, "model_name": None,
        "context_cards": None, "current_revision": 0, "format_version": 1,
        "revisions": [], "artifact_status": {}}), encoding="utf-8")

    meta = SessionService().create_session("Some request.", slug="con")
    assert meta.session_id == "original-id"


def test_reserved_windows_device_names_are_refused_as_filename_stems():
    # validate_filename checks the stem before the first dot, so `con.md` and `con.tar.gz` are equally reserved -- Windows refuses `CreateFile` on the device name regardless of extension.
    from requivo.core.errors import InvalidFilenameError
    from requivo.core.persistence import validate_filename
    for bad in ("con.md", "CON.MD", "nul.txt", "lpt1.json", "com9.tar.gz"):
        with pytest.raises(InvalidFilenameError):
            validate_filename(bad)
    # Must-not-fire control: an ordinary artifact filename, and one that merely starts with the reserved word as a substring rather than the whole stem, stay valid.
    for ok in ("prd.md", "console.md", "config.md"):
        assert validate_filename(ok) == ok


# ── the loader that reads a model back (#204) ────────────────────────────────


def test_load_model_rejects_invalid_model(tmp_path):
    """Still a refusal, and no longer a `ValidationError` reaching the caller (#204)."""
    bad = tmp_path / "model.json"
    bad.write_text(json.dumps({"questions": [], "summary": {}}))  # required `model` missing
    with pytest.raises(ModelUnreadableError) as ei:
        load_model(bad)
    assert isinstance(ei.value, RequivoError), "a traceback here is the bug, not the guard"
    assert ei.value.details == {"path": str(bad)}, (
        "a bare model.json has no session and no revision; padding those keys with nulls would "
        "state facts nobody measured (see the family note in docs/compatibility.md)"
    )


@pytest.mark.parametrize("corruption", [
    "",                                            # empty
    "{",                                           # truncated mid-object
    '{"model": {}, "questions": [], "summary"',    # truncated after a valid prefix
    "not json at all",
])
def test_a_corrupt_model_is_a_structured_error_from_every_door(workspace, corruption):
    """Four ways of being corrupt, and -- the load-bearing half -- every door into a model."""
    svc = SessionService()
    slug = "corrupt-model"
    svc.create_session("A leave approval system.", slug=slug)
    svc.update_model(slug, _full_model())
    d = store.canonical_dir(slug)

    for target in (d / "model.json", d / "revisions" / "0001-model.json"):
        target.write_text(corruption, encoding="utf-8")

    for call in (lambda: store.load_session_model(slug),
                 lambda: store.load_revision_model(slug, 1),
                 lambda: load_model(d / "model.json")):
        with pytest.raises(ModelUnreadableError) as ei:
            call()
        assert str(d) in str(ei.value), "the message names the file that could not be read"

    # The two that know which session they are reading say so, and say where the history is.
    with pytest.raises(ModelUnreadableError) as ei:
        store.load_session_model(slug)
    msg = str(ei.value)
    assert f"requivo session verify {slug}" in msg
    assert "revisions/" in msg, "the remedy was on disk the whole time and nothing said so"
    assert ei.value.details["slug"] == slug

    with pytest.raises(ModelUnreadableError) as ei:
        store.load_revision_model(slug, 1)
    assert ei.value.details["revision"] == 1


def test_a_missing_model_is_not_reported_as_a_corrupt_one(workspace):
    """The distinction the wrapping must not flatten."""
    SessionService().create_session("A leave approval system.", slug="no-model-yet")
    with pytest.raises(SessionNotFoundError):
        store.load_session_model("no-model-yet")   # revision 0: no model.json has been written
