"""ArtifactService — save, list, and read generated artifacts against a session.

An artifact is a *view* of the model at a specific revision. This service records that provenance
(source revision) when an artifact is saved, reports each artifact's freshness relative to the current
model, and can flag the blast radius stale after a model change. It never generates content — the
provider (or Claude Code) produces the text; this service persists and tracks it.
"""

from __future__ import annotations

import logging

from requivo.core.dependencies import ARTIFACT_FILENAMES, REASONING_CONSUMERS, diff_models, diff_reasoning, propagate
from requivo.core.errors import InvalidSessionError, RequivoError, SessionNotFoundError
from requivo.core.persistence import ArtifactStatus
from requivo.services.repository import SessionRepository, default_repository

logger = logging.getLogger(__name__)

# The saveable artifact vocabulary (type → filename under <session>/artifacts/) lives in Core, where
# the CLI's `--type` choices and the integrity checker read the same one. Re-exported here because
# this is where callers expect to find it.


class UnknownArtifactTypeError(RequivoError):
    code = "unknown_artifact_type"


class UnstatedSourceRevisionError(InvalidSessionError):
    """`save` was called without the revision the artifact was reasoned from.

    Only the caller knows what it read. This service can see the session's *current* revision, which
    is a different fact, and reading one as the other is the whole of #6: the omitted revision was
    filled in with the current one and the freshness question was then answered `False` without the
    dependency graph being consulted at all. The recorded number was a real revision of a real
    session, so nothing downstream could ever tell it from a stated one.

    Refused rather than guessed in either direction. `stale=False` claims the artifact is current
    when nobody established that; `stale=True` claims it is out of date when it may be perfectly
    fresh, and a flag that fires on every unstated save is one every reader learns to scroll past.
    The third state is neither flag — it is that the record does not get written, which is the answer
    `_stale_since` already gives for the sibling case where the history cannot be read.

    **The code is its own since #57.** `invalid_session` names the session rather than the omission,
    and it was inherited only because a new code needs a row in `requivo/http.py::STATUS_BY_CODE`
    (`web/app.py::_STATUS_BY_CODE` at the time -- moved by #422), which
    `test_every_error_code_has_an_explicit_http_status` requires of every code in the vocabulary — and
    the file it lived in was held by another lane in the round #6 landed. The precision
    sat in the *type* meanwhile, which a caller reading a serialized envelope cannot see: the one
    handle it had could not tell *you left a flag off* from *this session is broken*. The subclassing
    stays, so `except InvalidSessionError` still catches both arms.

    **The `details` shape is still shared, and that is now a decision rather than an obligation.**
    Both raise sites carry all five of `{slug, type, source_revision, current_revision, cause}`, two
    of them `null` here. While the code was shared the sharing was owed — a key present on one payload
    and absent on the other is precisely what #35 cost, a consumer matching the code, reading the key,
    and getting a `KeyError` from a payload that correctly carried the code it matched. With the codes
    split that debt is discharged — and narrowing this payload to the four keys it strictly needs
    would still break that consumer, for nothing, in the same release that finally told it the two
    arms are distinguishable. #52 answered the same question the same way in `docs/compatibility.md`:
    `opaque_origin` and `origin_mismatch` share a `details` shape and are still two codes, because a
    shared shape is not a shared meaning.
    `tests/test_artifact_provenance.py` asserts the two key sets against each other, so the kept shape
    is checked rather than remembered.
    """

    code = "unstated_source_revision"


class UnreadableSourceRevisionError(InvalidSessionError):
    """`save` stated a source revision and the history at that revision cannot be read.

    The sibling of the above, and the arm that kept `invalid_session` when #57 split the other one —
    which left the pair distinguishable in exactly one direction. #82 finishes it: both arms of "we
    cannot establish provenance" now carry a code that names which arm it is.

    `details` is the same five keys, `{slug, type, source_revision, current_revision, cause}`, all
    populated here where two are `null` on the sibling. That sharing is deliberate and is argued in
    `UnstatedSourceRevisionError` above: a shared shape is not a shared meaning, and narrowing either
    payload now would break a consumer for nothing.

    Answers 500, not 400. The caller stated a revision and it was a real one; the session's history is
    what is incomplete, and nothing the caller could have sent would have avoided it.
    """

    code = "unreadable_source_revision"


class ArtifactService:
    def __init__(self, repo: SessionRepository | None = None):
        self.repo: SessionRepository = repo or default_repository()

    def _filename(self, artifact_type: str) -> str:
        try:
            return ARTIFACT_FILENAMES[artifact_type]
        except KeyError as e:
            raise UnknownArtifactTypeError(
                f"unknown artifact type {artifact_type!r}; known: {', '.join(sorted(ARTIFACT_FILENAMES))}",
                details={"type": artifact_type},
            ) from e

    def save(self, slug: str, artifact_type: str, content: str,
             source_revision: int | None = None) -> ArtifactStatus:
        """Persist an artifact and tie it to the model revision it was generated from.
        `source_revision` is **required**: it is the one fact only the caller holds.

        An artifact reasoned from an *older* revision is saved with its freshness already computed
        against the current model, not assumed fresh. A long generation can finish after the session
        has moved (that is why generators capture their revision up front), and Claude Code can save a
        file it produced several turns ago — in both cases the honest answer is knowable: diff the
        source revision against the current model and see whether this artifact's dependencies were
        touched. Recording it fresh because the caller said so was how a stale PRD stayed unflagged.

        Omitting it used to mean "the current revision" (#6), which came out `stale=False` every time
        because a source revision that *is* the current one cannot have moved;
        `test_an_omitted_source_revision_is_refused_rather_than_read_as_now` is the guard. The `None`
        default stays so the omission arrives as a structured `UnstatedSourceRevisionError` a surface
        can print rather than a `TypeError` traceback — the CLI passes `--revision` straight through."""
        filename = self._filename(artifact_type)
        if not self.repo.has_meta(slug):
            raise SessionNotFoundError(
                f"session '{slug}' is not in the canonical store; apply a model first", details={"slug": slug})
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)
            if source_revision is None:
                # Before any write: a refused save must leave neither a file under artifacts/ nor a
                # status row in session.json, or the next reader finds content nothing describes.
                raise UnstatedSourceRevisionError(
                    f"cannot record {artifact_type!r} against session '{slug}': the revision it was "
                    "reasoned from was not stated, so whether it is current cannot be established — "
                    f"and this session is at revision {meta.current_revision}, which is not evidence "
                    "about what the content was generated from. State it: `source_revision=N` on the "
                    "service, `--revision N` on `requivo artifact save`. This session has revisions "
                    f"1..{meta.current_revision or 0}.",
                    details={"slug": slug, "type": artifact_type, "source_revision": None,
                             "current_revision": meta.current_revision,
                             # `cause` is present and null rather than absent, and since #57 that is
                             # a kept shape rather than a required one. This arm has its own code now,
                             # so `docs/compatibility.md`'s rule — one code, one `details` shape — no
                             # longer forces it to match `_stale_since`'s payload key for key. It
                             # matches anyway: dropping the key buys nothing, and a consumer reading
                             # `details["cause"]` across both arms would get the `KeyError` that #35
                             # cost us — in the very release that told it the two arms are finally
                             # distinguishable. There is no underlying failure to name here, and
                             # `null` says that.
                             "cause": None})
            stale = self._stale_since(slug, artifact_type, source_revision, meta.current_revision)
            result = self.repo.save_artifact(slug, artifact_type, filename, content,
                                             source_revision=source_revision, stale=stale)
            logger.info("artifact saved: slug=%s type=%s source_revision=%d stale=%s",
                       slug, artifact_type, source_revision, stale)
            return result

    def _stale_since(self, slug: str, artifact_type: str, source_revision: int,
                     current_revision: int) -> bool:
        """Whether an artifact generated from `source_revision` is already out of date at
        `current_revision` — the same dependency-graph question `update_model` answers, asked after
        the fact. False when the source *is* the current revision: nothing has moved since.

        An unreadable history is refused rather than answered. This used to return False, on the
        reasoning that an unanswerable question must not manufacture a stale flag — but `False` is not
        the absence of an answer, it is the claim "this artifact is up to date", and it was being made
        about a session whose history could not be read at all. Both directions invent something; only
        one of them is silent. The honest outcome is that the save does not happen, because the
        provenance it would record cannot be verified.

        **The guard catches the failure set, not one family of it (#6 F2).** It caught `RequivoError`
        alone, which covers exactly one way a revision fails to load: `load_revision_model` raises
        `SessionNotFoundError` for a file that is *absent*. A file that is present and unreadable took
        every other route out — a truncated `0002-model.json` from an interrupted sync reaches
        `PersistedEngineOutput.model_validate_json` and raises pydantic's `ValidationError` (a `ValueError`),
        a revision that fails to decode raises `UnicodeDecodeError` (also a `ValueError`), and a
        permission or device error raises `OSError`. None of the three is a `RequivoError`, so the
        one block that exists to turn "I cannot establish freshness" into a refusal never ran, and a
        raw traceback came out of a service call from inside the session lock — past `cli.py`'s
        `except RequivoError` too, so the surface could not phrase it either. The three are caught by
        their base classes rather than by name so the next reader of a corrupt file joins them; the
        `try` wraps only the two loads, so a defect in the diff below still surfaces as itself.

        **What this used to leave open, and no longer does (#11).** The decode arm only fires if the
        decode actually *raises*, and that is decided one layer down, in `load_revision_model` /
        `load_session_model`. Those two called `p.read_text()` with no explicit encoding while
        `_atomic_write` writes the same files as UTF-8, so where the locale default is not UTF-8 —
        cp1252 on a default Windows install — most UTF-8 byte sequences decode to *something* rather
        than failing: a revision holding an em-dash came back mojibaked, this guard saw nothing to
        catch, and `_stale_since` answered from quietly corrupted text. Both reads name
        `encoding="utf-8"` now, as does every other text read in the package, and
        `tests/test_encoding.py` fails the build if one loses it again. The paragraph is kept rather
        than deleted because the *shape* is the point: this refusal can only catch a decode that
        raises, so a decode that silently succeeds on the wrong codepage bypasses it entirely."""
        if source_revision >= current_revision:
            return False
        if source_revision < 1:
            return False  # out of range — `save_session_artifact` refuses it with the precise message
        try:
            was = self.repo.load_revision(slug, source_revision)
            now = self.repo.load_model(slug)
        except (RequivoError, ValueError, OSError) as e:
            # The cause is named by type as well as text: a pydantic ValidationError's message says
            # nothing about *why* a file could not be read, and "invalid JSON" and "no such revision"
            # are different remedies. The full text goes in `details`; the message carries its first
            # line, because a multi-line pydantic report inside a sentence is unreadable in a terminal.
            cause = f"{type(e).__name__}: {e}"
            raise UnreadableSourceRevisionError(
                f"cannot establish whether this {artifact_type!r} is current: session '{slug}' is at "
                f"revision {current_revision} but revision {source_revision}, the one it was reasoned "
                f"from, cannot be read ({(cause.splitlines() or [cause])[0]}). The session's history is "
                "incomplete — verify it before recording an artifact against it.",
                details={"slug": slug, "source_revision": source_revision,
                         "current_revision": current_revision, "type": artifact_type,
                         "cause": cause},
            ) from e
        if diff_reasoning(was, now).changed and artifact_type in REASONING_CONSUMERS:
            return True
        return artifact_type in set(propagate(now, diff_models(was, now)).artifacts)

    def list(self, slug: str) -> dict[str, dict]:
        """Every recorded artifact with its freshness relative to the current model revision."""
        meta = self.repo.read_meta(slug)
        out: dict[str, dict] = {}
        for t, st in meta.artifact_status.items():
            # Freshness is the explicit stale flag, set by `update_model`/`mark_stale` for exactly the
            # artifacts in a change's blast radius. The source revision is provenance only — an artifact
            # is NOT stale merely because the model moved on; a change that misses its dependencies
            # leaves it fresh (the whole point of the dependency graph).
            out[t] = {"revision": st.revision, "filename": st.filename,
                      "updated_at": st.updated_at, "stale": st.stale}
        return out

    def show(self, slug: str, artifact_type: str) -> str:
        """The saved content of an artifact."""
        filename = self._filename(artifact_type)
        content = self.repo.load_artifact(slug, filename)
        if content is None:
            raise SessionNotFoundError(
                f"session '{slug}' has no saved {artifact_type!r} artifact",
                details={"slug": slug, "type": artifact_type})
        return content

    def show_with_status(self, slug: str, artifact_type: str) -> tuple[str, dict]:
        """One coherent read of an artifact's saved content and its freshness row -- the two facts
        the HTTP API's artifact envelope reports together (#425): `{type, filename, source_revision,
        updated_at, stale, content}`.

        `show()` and `list()`, called separately, are two reads at two different instants, and a
        regeneration landing between them hands back content from one revision beside a freshness
        row describing another, undetectably -- invariant 12's shape, one layer over, for a plain
        read. This takes the lock once and reads both under it;
        `test_show_with_status_is_not_interleaved_by_a_concurrent_save` is the guard.

        Raises `SessionNotFoundError` if nothing has ever been saved under `artifact_type` -- the
        same refusal `show()` raises alone."""
        filename = self._filename(artifact_type)
        with self.repo.lock(slug):
            content = self.repo.load_artifact(slug, filename)
            if content is None:
                raise SessionNotFoundError(
                    f"session '{slug}' has no saved {artifact_type!r} artifact",
                    details={"slug": slug, "type": artifact_type})
            meta = self.repo.read_meta(slug)
        status = meta.artifact_status.get(artifact_type)
        row = ({"revision": status.revision, "filename": status.filename,
                "updated_at": status.updated_at, "stale": status.stale} if status else {})
        return content, row

    def mark_stale(self, slug: str, changed_slots: list[str]) -> list[str]:
        """Flag every generated artifact in the blast radius of `changed_slots` stale, and return the
        types flagged. Used after a model change made outside `update_model`.

        Read-modify-write on the metadata, so it runs under the session lock like every other compound
        mutation: a concurrent writer landing between the read and the write would have its own flags
        reverted by ours."""
        with self.repo.lock(slug):
            model = self.repo.load_model(slug)
            meta = self.repo.read_meta(slug)
            hit = set(propagate(model, changed_slots).artifacts) & set(meta.artifact_status)
            for t in hit:
                meta.artifact_status[t].stale = True
            if hit:
                self.repo.write_meta(slug, meta)
            return sorted(hit)
