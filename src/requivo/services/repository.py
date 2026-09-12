"""SessionRepository — the storage seam under the service layer.

`SessionService` and `ArtifactService` own the *orchestration* (validate → diff → propagate → revision
→ stale-flag → readiness); where a session physically lives is a separate concern. This module draws
that line: a `SessionRepository` protocol names exactly the storage operations the services need, and
`FileSessionRepository` implements them against the `.requivo/sessions/` layout (delegating to
`core.persistence`, so on-disk behaviour is unchanged).

The point is a non-filesystem backing: a deployment can supply a `PostgresSessionRepository` with the
same protocol and reuse the service orchestration verbatim, instead of bypassing the service or faking
a filesystem. The protocol is deliberately backing-neutral.

The pre-0.9.8 file backing carried a second store: every read fell back to a legacy `out/<slug>/`
session, and a mutation migrated one in place. That kept old sessions working without the user
knowing — which is also what was wrong with it. The fallback ran on every read of every session for
the benefit of a layout nothing has written since 0.8.0, and it made "where does this session live?"
a question with two answers everywhere in the code. Migration is now explicit (`requivo session
migrate`), and all that remains here is *detection*: a legacy directory turns "no session" into an
error that names the command to run.
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
    """The storage operations the service layer depends on — nothing more. Implementations map these
    onto a concrete backing (files today, Postgres elsewhere). Every method keys on a validated slug."""

    def lock(self, slug: str) -> AbstractContextManager[None]:
        """Hold exclusive write access to a session for the duration of the block.

        A model update is several storage calls — read the metadata, save a revision, rewrite the
        artifact flags — and it is only correct if no other writer can interleave with them. The
        service takes this lock around the whole sequence rather than trusting each call to be
        individually safe. Implementations must be re-entrant within a thread, since the calls inside
        the block take it again. A file backing maps this to an OS file lock; a Postgres backing maps
        it to the row lock of the enclosing transaction."""
        ...

    def exists(self, slug: str) -> bool:
        """True if a usable session exists."""
        ...

    def has_meta(self, slug: str) -> bool:
        """True if the session is in the mutation-backed store — i.e. `read_meta` would succeed. Kept
        distinct from `exists` because a backing may hold a session it cannot yet describe."""
        ...

    def ensure_writable(self, slug: str) -> None:
        """Prepare a session for its first mutation, raising `SessionNotFoundError` if there is none."""
        ...

    def create(self, slug: str, request: str, *, provider: Optional[str] = None,
               model_name: Optional[str] = None, context_cards: Optional[list[str]] = None) -> SessionMeta:
        """Create a fresh session (no model yet, revision 0) and return its metadata."""
        ...

    def delete(self, slug: str) -> None:
        """Irreversibly remove a session, raising `SessionNotFoundError` if there is none (#238).

        On the protocol, not only the file backing: a hosted (e.g. Postgres) backing needs the
        identical operation, and there was otherwise no method for it to implement -- the same
        reasoning that keeps every other mutation here rather than only on `FileSessionRepository`.
        A backing that claims a slug atomically on `create` (invariant 11) must release it just as
        atomically here: once this returns, `exists(slug)` is False and `create(slug, ...)` for the
        identical slug must succeed as though nothing had ever occupied it --
        `test_delete_removes_the_session_from_the_backing` and
        `test_deleting_then_recreating_the_same_slug_succeeds`, on the shared
        `requivo.testing.repository_conformance.SessionRepositoryConformance` every backing runs."""
        ...

    def read_meta(self, slug: str) -> SessionMeta:
        """The session metadata, raising `SessionNotFoundError` if the session has none."""
        ...

    def write_meta(self, slug: str, meta: SessionMeta) -> None:
        """Persist replacement metadata for a session."""
        ...

    def list_slugs(self) -> list[str]:
        """Every session slug in the mutation-backed store, sorted. **Names known to be sessions** —
        see `list_unexaminable` for the ones the backing could not decide about."""
        ...

    def list_unexaminable(self) -> list[UnexaminableEntry]:
        """Every name the backing found and could **not** decide about, with the reason (#80).

        The third answer to *what is in the store?*, and it exists because the other two are both
        claims. Omitting the entry says nothing is wrong and loses it; listing it as a slug says it
        is a session, which is what the backing has just failed to establish. So it comes back as
        itself, and the service turns it into a degraded row.

        On the file backing this is a directory the process cannot stat into. A backing on which the
        question cannot arise — one whose enumeration either yields a row or raises for the whole
        query — returns `[]`, and that is an honest *we looked and there was nothing*. What no
        backing may do is drop a row it enumerated and could not decode: the point of the method is
        that the caller can tell those two apart, and a silent drop is the absence this whole
        listing path exists to end.

        Failing to enumerate **at all** is not this — it is the aggregate having no members to speak
        for, and it raises, exactly as `list_slugs` does. `test_the_repository_exposes_the_third_bucket`
        pins the file backing; `test_known_slugs_and_unexaminable_entries_do_not_overlap` is the
        conformance bar every backing runs."""
        ...

    def load_model(self, slug: str) -> EngineOutput:
        """The current model, raising `SessionNotFoundError` if there is none."""
        ...

    def load_revision(self, slug: str, revision: int) -> EngineOutput:
        """A historical model revision, raising `SessionNotFoundError` if it does not exist. Needed to
        answer "what changed since the model this artifact was generated from?" after the fact."""
        ...

    def save_revision(self, slug: str, model: EngineOutput, *, expected_revision: Optional[int] = None,
                      provenance: Optional[dict] = None) -> tuple[int, SessionMeta]:
        """Persist a new model revision (with optimistic-lock precondition and provenance) and return
        `(new_revision, updated_meta)`."""
        ...

    def request_text(self, slug: str) -> str:
        """The originating request text (empty string if none)."""
        ...

    def context_cards(self, slug: str) -> Optional[list[str]]:
        """The context-card selection recorded for the session (None == all cards)."""
        ...

    def save_artifact(self, slug: str, artifact_type: str, filename: str, content: str, *,
                      source_revision: int, stale: bool = False) -> ArtifactStatus:
        """Persist a generated artifact and record its provenance (source revision) and freshness.
        `stale` is decided by the service from the dependency graph, never by the storage layer."""
        ...

    def load_artifact(self, slug: str, filename: str) -> Optional[str]:
        """The saved content of an artifact file, or None if it is not present.

        **None means absent, and only that.** A backing that derives a *location* from `filename` —
        a path on the file backing, a key with any namespacing of its own — must validate it and
        **raise** on a name it will not accept, never return None: a caller that cannot tell a
        refusal from an absence has been handed the wrong answer in the more dangerous direction,
        and a rejected traversal then reads as an artifact nobody has generated yet.

        A backing for which `filename` is an *opaque* key has nothing to refuse, and a miss there is
        a real absence — `InMemorySessionRepository` in the tests is that case, and returns None
        correctly. The obligation follows the path-building, not the protocol."""
        ...


class FileSessionRepository:
    """The default backing: the `.requivo/sessions/<slug>/` layout. A thin adapter over
    `core.persistence.Store` -- every method below resolves *which* `Store` to call through
    `_resolve_store()`, and that is what makes this class genuinely addressable by construction
    (#272), which it was not before: see `docs/cloud-boundary.md` (§3.1).

    **`root=None` (the default) is ambient, resolved fresh on every call -- not frozen at
    construction.** This is the asymmetry the whole class turns on, so it is worth stating twice:
    `_resolve_store()` builds a new `Store(paths.workspace_root())` each time it is asked, exactly
    like every `core.persistence` module-level function already did before this class existed --
    which is what keeps `cli.py`'s `--workspace` handling (`os.environ["REQUIVO_WORKSPACE"] = ...`,
    mutated *after* argument parsing, before any repository is used) working unchanged: the next
    call picks the mutation up, because nothing was cached at construction to go stale.

    **An explicit `root`, given at construction, is fixed state instead.** `_resolve_store()` then
    returns the *same* `Store` object on every call, so this instance addresses exactly the
    workspace it was built with, immune to any later environment change -- including a concurrent
    call elsewhere in the process mutating `REQUIVO_WORKSPACE` for a *different* repository's sake.
    This is the property the class used to lack entirely: two instances constructed with two
    explicit roots are now genuinely independent and addressable at once, which is what makes
    `DiscoveryService`'s own "one repository per service, chosen once" comment (`services/
    discovery.py`) an enforceable claim on this backing rather than an aspiration. Pinned by
    `test_two_repositories_against_two_roots_are_independent_in_one_process`."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._store: Optional[Store] = Store(root) if root is not None else None

    def _resolve_store(self) -> Store:
        """See the class docstring for the ambient-vs-fixed distinction this exists to hold."""
        return self._store if self._store is not None else Store(workspace_root())

    def store(self) -> Store:
        """The `Store` this repository addresses right now. Not part of the `SessionRepository`
        protocol -- a Postgres backing has no filesystem root to expose -- so this is specific to
        the file backing, the same way `FileSessionRepository`'s other filesystem-only surface
        (`canonical_dir`, `artifact_path`, …) already is, per `tests/test_boundaries.py`'s own
        allowlist reasoning for those. Exists so a caller that needs a *root*, not a session
        operation, can address the same workspace this repository does instead of falling back to
        a second, independent ambient read: `DiscoveryService`'s own discovery-guard path and
        reserved-slug probe, and `SessionService.no_session`'s error text, all read the workspace
        root outside any repository method and would otherwise silently disagree with an explicit
        `root=` the moment one is given (#272's scope amendment)."""
        return self._resolve_store()

    def _missing(self, slug: str) -> SessionNotFoundError:
        """The one place "there is no such session" is phrased — including the case where there *is*
        one, in the retired `out/` layout, which is a different problem with a specific answer."""
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
               model_name: Optional[str] = None, context_cards: Optional[list[str]] = None) -> SessionMeta:
        return self._resolve_store().create_session(slug, request, provider=provider,
                                                     model_name=model_name, context_cards=context_cards)

    def delete(self, slug: str) -> None:
        self._resolve_store().delete_session(slug)

    def read_meta(self, slug: str) -> SessionMeta:
        return self._resolve_store().read_meta(slug)

    def write_meta(self, slug: str, meta: SessionMeta) -> None:
        self._resolve_store().write_meta(slug, meta)

    def list_slugs(self) -> list[str]:
        return self._resolve_store().list_session_slugs()

    def list_unexaminable(self) -> list[UnexaminableEntry]:
        # A second scan rather than one shared with `list_slugs`, deliberately. The two are two
        # instants and a session appearing between them can land in neither answer — the same race
        # `scan_session_root` exists to close for `doctor`. It is the lesser cost here: sharing one
        # scan means holding the partition's three parts as state on this repository, and the
        # answer would then be as old as the last call that happened to populate it, on a class
        # whose whole point (#272) is that different calls can legitimately address different
        # stores. `session list` reads both within microseconds of each other regardless.
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
        # Delegated rather than re-joined here: this method built the artifacts/ path inline, which
        # is how the read side kept a traversal the write side had already closed at `artifact_path`.
        return self._resolve_store().read_artifact_file(slug, filename)


def default_repository() -> SessionRepository:
    """The repository the CLI uses — a file backing under the caller's workspace, resolved
    ambiently on every call (`root=None`) — see `FileSessionRepository`'s own docstring."""
    return FileSessionRepository()
