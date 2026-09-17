"""SessionRepository: the storage seam under the service layer. The protocol names exactly the
storage operations the services need; `FileSessionRepository` implements them over
`core.persistence`, and a Postgres backing reuses the orchestration verbatim. Migration from the
retired `out/` layout is explicit (`requivo session migrate`); only detection remains here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from requivo.core.contracts import EngineOutput
from requivo.core.errors import SessionNotFoundError
from requivo.core.persistence import ArtifactStatus, SessionMeta, Store, UnexaminableEntry
from requivo.paths import workspace_root


@runtime_checkable
class SessionRepository(Protocol):
    """The storage operations the service layer depends on, keyed on a validated slug."""

    def lock(self, slug: str) -> AbstractContextManager[None]:
        """Hold exclusive write access to a session for the block; re-entrant within a thread, since
        the calls inside take it again (invariant 9)."""
        ...

    def exists(self, slug: str) -> bool:
        """True if a usable session exists."""
        ...

    def has_meta(self, slug: str) -> bool:
        """True if the session is in the mutation-backed store, i.e. `read_meta` would succeed."""
        ...

    def ensure_writable(self, slug: str) -> None:
        """Prepare a session for its first mutation, raising `SessionNotFoundError` if there is none."""
        ...

    def create(self, slug: str, request: str, *, provider: Optional[str] = None,
               model_name: Optional[str] = None, context_cards: Optional[list[str]] = None,
               perimeter: Optional[str] = None) -> SessionMeta:
        """Create a fresh session (revision 0) and return its metadata; `perimeter` (#608) is frozen here."""
        ...

    def delete(self, slug: str) -> None:
        """Irreversibly remove a session, raising `SessionNotFoundError` if there is none (#238). Once
        this returns, `create` for the same slug must succeed as though nothing had occupied it:
        `test_deleting_then_recreating_the_same_slug_succeeds` on the shared conformance suite."""
        ...

    def read_meta(self, slug: str) -> SessionMeta:
        """The session metadata, raising `SessionNotFoundError` if the session has none."""
        ...

    def write_meta(self, slug: str, meta: SessionMeta) -> None:
        """Persist replacement metadata for a session."""
        ...

    def list_slugs(self) -> list[str]:
        """Every session slug in the mutation-backed store, sorted: names known to be sessions."""
        ...

    def list_unexaminable(self) -> list[UnexaminableEntry]:
        """Every name the backing found and could not decide about, with the reason (#80): the third
        answer, which the service turns into a degraded row. A backing on which the question cannot
        arise returns `[]`; none may drop a row it enumerated. Failing to enumerate at all raises.
        `test_known_slugs_and_unexaminable_entries_do_not_overlap` is the conformance bar."""
        ...

    def load_model(self, slug: str) -> EngineOutput:
        """The current model, raising `SessionNotFoundError` if there is none."""
        ...

    def load_revision(self, slug: str, revision: int) -> EngineOutput:
        """A historical model revision, raising `SessionNotFoundError` if it does not exist."""
        ...

    def save_revision(self, slug: str, model: EngineOutput, *, expected_revision: Optional[int] = None,
                      provenance: Optional[dict] = None) -> tuple[int, SessionMeta]:
        """Persist a new model revision (optimistic-lock precondition, provenance); returns `(new_revision, updated_meta)`."""
        ...

    def request_text(self, slug: str) -> str:
        """The originating request text (empty string if none)."""
        ...

    def context_cards(self, slug: str) -> Optional[list[str]]:
        """The context-card selection recorded for the session (None == all cards)."""
        ...

    def save_artifact(self, slug: str, artifact_type: str, filename: str, content: str, *,
                      source_revision: int, stale: bool = False) -> ArtifactStatus:
        """Persist a generated artifact with its source revision and freshness; `stale` is the service's decision."""
        ...

    def load_artifact(self, slug: str, filename: str) -> Optional[str]:
        """The saved content of an artifact file, or None if absent, and only absent: a backing that
        derives a location from `filename` must raise on a name it will not accept, never return None."""
        ...


class FileSessionRepository:
    """The default backing over `core.persistence.Store`. `root=None` is ambient, resolved fresh on
    every call, so `cli.py`'s `--workspace` env mutation is picked up; an explicit `root` is fixed
    state, immune to later environment changes (#272):
    `test_two_repositories_against_two_roots_are_independent_in_one_process`."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._store: Optional[Store] = Store(root) if root is not None else None

    def _resolve_store(self) -> Store:
        """The ambient-vs-fixed distinction the class docstring states."""
        return self._store if self._store is not None else Store(workspace_root())

    def store(self) -> Store:
        """The `Store` this repository addresses right now; file-backing only, for a caller that needs
        a root rather than a session operation (#272)."""
        return self._resolve_store()

    def _missing(self, slug: str) -> SessionNotFoundError:
        """The one place "there is no such session" is phrased, the retired `out/` layout included."""
        if self._resolve_store().legacy_exists(slug):
            return SessionNotFoundError(
                f"'{slug}' exists only in the retired out/ layout. Bring it into the session store "
                "with `requivo session migrate`, which converts every out/ session in one pass and "
                "leaves the originals in place.",
                details={"slug": slug, "legacy": True})
        return SessionNotFoundError(f"no session '{slug}'", details={"slug": slug})

    @contextmanager
    def lock(self, slug: str) -> Iterator[None]:
        with self._resolve_store().session_lock(slug):
            yield

    def exists(self, slug: str) -> bool:
        return self._resolve_store().session_exists(slug)

    def has_meta(self, slug: str) -> bool:
        return self._resolve_store().session_exists(slug)

    def ensure_writable(self, slug: str) -> None:
        if not self._resolve_store().session_exists(slug):
            raise self._missing(slug)

    def create(self, slug: str, request: str, *, provider: Optional[str] = None,
               model_name: Optional[str] = None, context_cards: Optional[list[str]] = None,
               perimeter: Optional[str] = None) -> SessionMeta:
        return self._resolve_store().create_session(slug, request, provider=provider,
                                                     model_name=model_name, context_cards=context_cards,
                                                     perimeter=perimeter)

    def delete(self, slug: str) -> None:
        self._resolve_store().delete_session(slug)

    def read_meta(self, slug: str) -> SessionMeta:
        return self._resolve_store().read_meta(slug)

    def write_meta(self, slug: str, meta: SessionMeta) -> None:
        self._resolve_store().write_meta(slug, meta)

    def list_slugs(self) -> list[str]:
        return self._resolve_store().list_session_slugs()

    def list_unexaminable(self) -> list[UnexaminableEntry]:
        # A second scan rather than one shared with `list_slugs`: sharing would hold the partition as
        # state on a class whose calls may address different stores (#272).
        return self._resolve_store().list_unexaminable_entries()

    def load_model(self, slug: str) -> EngineOutput:
        resolved = self._resolve_store()
        if not resolved.session_exists(slug):
            raise self._missing(slug)
        return resolved.load_session_model(slug)

    def load_revision(self, slug: str, revision: int) -> EngineOutput:
        return self._resolve_store().load_revision_model(slug, revision)

    def save_revision(self, slug: str, model: EngineOutput, *, expected_revision: Optional[int] = None,
                      provenance: Optional[dict] = None) -> tuple[int, SessionMeta]:
        return self._resolve_store().save_revision(
            slug, model, expected_revision=expected_revision, provenance=provenance)

    def request_text(self, slug: str) -> str:
        resolved = self._resolve_store()
        return resolved.session_request(slug) if resolved.session_exists(slug) else ""

    def context_cards(self, slug: str) -> Optional[list[str]]:
        resolved = self._resolve_store()
        return resolved.read_meta(slug).context_cards if resolved.session_exists(slug) else None

    def save_artifact(self, slug: str, artifact_type: str, filename: str, content: str, *,
                      source_revision: int, stale: bool = False) -> ArtifactStatus:
        return self._resolve_store().save_session_artifact(
            slug, artifact_type, filename, content, source_revision=source_revision, stale=stale)

    def load_artifact(self, slug: str, filename: str) -> Optional[str]:
        # Delegated, so the read side goes through `artifact_path` like the write side.
        return self._resolve_store().read_artifact_file(slug, filename)


def default_repository() -> SessionRepository:
    """The repository the CLI uses: a file backing under the caller's workspace, resolved ambiently."""
    return FileSessionRepository()
