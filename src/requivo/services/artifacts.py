"""ArtifactService: save, list and read generated artifacts against a session. An artifact is a view
of the model at a revision; this records that provenance, reports freshness, and never generates.
"""

from __future__ import annotations

import logging

from requivo.core.dependencies import ARTIFACT_FILENAMES, REASONING_CONSUMERS, diff_models, diff_reasoning, propagate
from requivo.core.errors import InvalidSessionError, RequivoError, SessionNotFoundError
from requivo.core.persistence import ArtifactStatus
from requivo.services.repository import SessionRepository, default_repository

logger = logging.getLogger(__name__)

# The saveable artifact vocabulary lives in Core; re-exported here because callers expect it.


class UnknownArtifactTypeError(RequivoError):
    code = "unknown_artifact_type"


class UnstatedSourceRevisionError(InvalidSessionError):
    """`save` was called without the revision the artifact was reasoned from: refused, not guessed
    in either direction (#6). Its own code since #57 (a serialized envelope could not tell *you
    left a flag off* from *this session is broken*); still an `InvalidSessionError`. `details` keeps
    the five shared keys, two of them `null`, since a shared shape is not a shared meaning (#35)."""

    code = "unstated_source_revision"


class UnreadableSourceRevisionError(InvalidSessionError):
    """`save` stated a source revision and the history at that revision cannot be read (#82): the
    sibling of the above, with the same five `details` keys, all populated. Answers 500, not 400:
    the session's history is what is incomplete."""

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
        """Persist an artifact tied to the revision it was generated from. `source_revision` is
        required, the one fact only the caller holds; an older one is saved with its freshness
        computed against the current model (invariant 2). The `None` default exists so the omission
        is a structured refusal: `test_an_omitted_source_revision_is_refused_rather_than_read_as_now` (#6)."""
        filename = self._filename(artifact_type)
        if not self.repo.has_meta(slug):
            raise SessionNotFoundError(
                f"session '{slug}' is not in the canonical store; apply a model first", details={"slug": slug})
        with self.repo.lock(slug):
            meta = self.repo.read_meta(slug)
            if source_revision is None:
                # Before any write: a refused save leaves neither a file nor a status row.
                raise UnstatedSourceRevisionError(
                    f"cannot record {artifact_type!r} against session '{slug}': the revision it was "
                    "reasoned from was not stated, so whether it is current cannot be established — "
                    f"and this session is at revision {meta.current_revision}, which is not evidence "
                    "about what the content was generated from. State it: `source_revision=N` on the "
                    "service, `--revision N` on `requivo artifact save`. This session has revisions "
                    f"1..{meta.current_revision or 0}.",
                    details={"slug": slug, "type": artifact_type, "source_revision": None,
                             "current_revision": meta.current_revision,
                             # `cause` present and null: the shared `details` shape (#57, #35).
                             "cause": None})
            stale = self._stale_since(slug, artifact_type, source_revision, meta.current_revision)
            result = self.repo.save_artifact(slug, artifact_type, filename, content,
                                             source_revision=source_revision, stale=stale)
            logger.info("artifact saved: slug=%s type=%s source_revision=%d stale=%s",
                       slug, artifact_type, source_revision, stale)
            return result

    def _stale_since(self, slug: str, artifact_type: str, source_revision: int,
                     current_revision: int) -> bool:
        """Whether an artifact from `source_revision` is already out of date at `current_revision`:
        the dependency-graph question, asked after the fact. An unreadable history is refused rather
        than answered `False`, and the guard catches the whole failure set (pydantic's `ValueError`,
        `UnicodeDecodeError`, `OSError`), not `RequivoError` alone (#6); the `try` wraps only the
        two loads. A decode that silently succeeds on the wrong codepage bypasses this, which is why
        every read names its encoding (#11, invariant 16)."""
        if source_revision >= current_revision:
            return False
        if source_revision < 1:
            return False  # out of range — `save_session_artifact` refuses it with the precise message
        try:
            was = self.repo.load_revision(slug, source_revision)
            now = self.repo.load_model(slug)
        except (RequivoError, ValueError, OSError) as e:
            # The cause is named by type as well as text; the message carries its first line only.
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
            # Freshness is the explicit stale flag; the source revision is provenance only (invariant 1).
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
        """An artifact's saved content and its freshness row from one read under the lock, for the
        API's artifact envelope (#425, invariant 12): `test_show_with_status_is_not_interleaved_by_a_concurrent_save`.
        Raises `SessionNotFoundError` when nothing was saved under `artifact_type`."""
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
        """Flag every artifact in the blast radius of `changed_slots` stale and return the types; a
        read-modify-write under the session lock."""
        with self.repo.lock(slug):
            model = self.repo.load_model(slug)
            meta = self.repo.read_meta(slug)
            hit = set(propagate(model, changed_slots).artifacts) & set(meta.artifact_status)
            for t in hit:
                meta.artifact_status[t].stale = True
            if hit:
                self.repo.write_meta(slug, meta)
            return sorted(hit)
