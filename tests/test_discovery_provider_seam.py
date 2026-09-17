"""The `ReasoningProvider` seam is not Anthropic-shaped (a fake provider drives a whole discovery, and a
revision's provenance is enough to reproduce the call that produced it), a generation carries the revision
it read even when a concurrent write lands mid-flight (invariant 2, #424)."""
from __future__ import annotations

import contextlib
import json
import threading

import pytest
from _fakes import out, slot

from conftest import FakeProvider as _FakeProvider
from conftest import RacingClient as _RacingClient
from conftest import full_model as _full_model
from conftest import slot as _slot
from requivo.core.errors import RevisionConflictError as _RevConflict
from requivo.core.errors import SessionExistsError as _SessionExists
from requivo.core.errors import SessionNotFoundError as _NotFound
from requivo.core.persistence import ArtifactStatus, RevisionRecord, SessionMeta
from requivo.services.artifacts import ArtifactService
from requivo.services.repository import FileSessionRepository, SessionRepository
from requivo.services.sessions import SessionService
from requivo.testing.repository_conformance import SessionRepositoryConformance

# ── generation vs. concurrent writes (invariant 2) ────────────────────────────


def test_generation_that_races_a_concurrent_apply_does_not_lose_it(workspace):
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())          # revision 1 — what the generator will read

    def concurrent_answer():
        svc.update_model("s", _full_model(**{"business_rules": _slot(90, "explicit", "high", "HR signs off")}))

    brief_reply = json.dumps({"complexity": "medium", "problem": "P", "solution": "S",
                              "risks": [], "next_steps": []})
    disco = DiscoveryService(client=_RacingClient(brief_reply, concurrent_answer))
    with pytest.raises(RevisionConflictError):
        disco.generate("s", "brief")

    # The rule that landed mid-flight is still there — the assessment's apply did not write over it.
    assert svc.load_model("s").model["business_rules"].value == "HR signs off"


def test_an_answers_turn_holds_the_revision_it_read(workspace):
    # A turn has the same seam as a generation, so a caller that passes no expectation still gets one.
    from requivo.core.errors import RevisionConflictError
    from requivo.services.discovery import DiscoveryService

    svc = SessionService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", _full_model())

    def concurrent_apply():
        svc.update_model("s", _full_model(**{"risks": _slot(70, "explicit", "high", "rollout risk")}))

    reply = _full_model()
    reply["summary"] = {"objective": "A leave approval system"}   # a discovery reply owes an objective
    disco = DiscoveryService(client=_RacingClient(json.dumps(reply), concurrent_apply))
    with pytest.raises(RevisionConflictError):
        disco.answer("s", "here are my answers")
    assert svc.load_model("s").model["risks"].value == "rollout risk"


def test_an_answers_turn_that_says_nothing_about_reasoning_keeps_it(workspace):
    """The full user journey the tri-state exists for: discovery → assessment → an ordinary answer."""
    from requivo.services.discovery import DiscoveryService

    svc, art = SessionService(), ArtifactService()
    svc.create_session("Something.", slug="s")
    svc.update_model("s", {**_full_model(), "decisions": [
        {"decision": "Managers approve in-app", "derived_from": ["permissions"]}]})
    art.save("s", "prd", "# PRD\n", source_revision=1)

    reply = {**_full_model(**{"workflow": _slot(90, "explicit", "high", "request → approve")}),
             "summary": {"objective": "A leave approval system"}}
    DiscoveryService(client=_RacingClient(json.dumps(reply), lambda: None)).answer("s", "in-app")

    after = svc.load_model("s")
    assert [d.decision for d in after.decisions] == ["Managers approve in-app"]
    assert after.model["workflow"].value == "request → approve"   # the facts did move
    assert art.list("s")["prd"]["stale"] is True                  # …and that alone marks the PRD stale


# ── the provider seam ─────────────────────────────────────────────────────────


class _NamelessProvider:
    """Implements every member `ReasoningProvider` *declares* — and nothing more."""

    def analyze(self, request, *, current_model=None, answers=None, only=None, perimeter=None):
        raise AssertionError("a provider missing `name` must fail before it is asked to reason")

    def generate(self, artifact_type, model, *, only=None):
        raise AssertionError("not reached")

    def model_name(self):
        return "nameless-1"

    def provenance(self, op, *, only=None, perimeter=None):
        return {"provider": "nameless", "model_name": self.model_name(), "prompt_version": "sha256:x"}


def test_discovery_runs_on_a_provider_that_is_not_anthropic(workspace):
    from requivo.services.discovery import DiscoveryService

    slug = DiscoveryService(_FakeProvider()).start("A leave approval system.", slug="fake-prov")
    meta = SessionService().meta(slug)
    # Nothing hard-codes "anthropic": the session and its revision are stamped by the provider itself.
    assert meta.provider == "fake" and meta.model_name == "fake-model-1"
    assert [(r.provider, r.model_name) for r in meta.revisions] == [("fake", "fake-model-1")]


def test_the_provider_protocol_declares_every_member_the_orchestration_reads(workspace):
    """`provider.name` is read on the first discovery, so it is part of the contract or the contract is not
    the contract."""
    from requivo.providers.anthropic import AnthropicProvider
    from requivo.providers.base import ReasoningProvider
    from requivo.services.discovery import DiscoveryService

    # Must-fire half: real conformers satisfy the protocol.
    assert isinstance(_FakeProvider(), ReasoningProvider)
    assert isinstance(AnthropicProvider.__new__(AnthropicProvider), ReasoningProvider)  # no API key needed

    # Must-not-fire half: everything declared, `name` absent.
    assert not isinstance(_NamelessProvider(), ReasoningProvider)

    # …and the positive control that keeps the line above from being about a decorative member.
    with pytest.raises(AttributeError, match="name"):
        DiscoveryService(_NamelessProvider()).start("A leave approval system.", slug="nameless")


def test_a_revision_records_the_prompt_it_was_reasoned_against(workspace):
    """Invariant 6: provenance is real or absent (#286)."""
    # A revision log that is only "anthropic, at 14:02" cannot reproduce anything.
    from requivo.providers.anthropic import prompt_version
    from requivo.services.discovery import DiscoveryService

    reply = {**_full_model(), "summary": {"objective": "A leave approval system"}}
    slug = DiscoveryService(client=_RacingClient(json.dumps(reply), lambda: None)).start(
        "A leave approval system.", slug="prov")

    rec = SessionService().meta(slug).revisions[-1]
    assert rec.provider == "anthropic" and rec.model_name
    assert rec.prompt_version and rec.prompt_version.startswith("sha256:")
    # It follows the context-card selection, because a different card set is different reasoning (#13).
    assert prompt_version("analyze") != prompt_version("analyze", only=["b2b-platform"])


# ── SessionRepository: the storage seam (proves the service is backing-agnostic, #424) ──


class InMemorySessionRepository:
    """A dict-backed SessionRepository — no filesystem, no `.requivo/` directory."""

    def __init__(self):
        self._meta: dict = {}
        self._model: dict = {}
        self._revs: dict = {}      # (slug, revision) → model, the history a file backing keeps on disk
        self._req: dict = {}
        self._art: dict = {}
        self._locks: dict = {}                 # slug -> threading.RLock, created on first use
        self._locks_guard = threading.Lock()    # protects _locks itself, not any session's data

    def _lock_for(self, slug):
        # threading.RLock is re-entrant *per thread* by construction (#424).
        with self._locks_guard:
            if slug not in self._locks:
                self._locks[slug] = threading.RLock()
            return self._locks[slug]

    @contextlib.contextmanager
    def lock(self, slug):
        lk = self._lock_for(slug)
        lk.acquire()
        try:
            yield
        finally:
            lk.release()

    def exists(self, slug): return slug in self._meta
    def has_meta(self, slug): return slug in self._meta

    def ensure_writable(self, slug):
        if slug not in self._meta:
            raise _NotFound(f"no session '{slug}'", details={"slug": slug})

    def create(self, slug, request, *, provider=None, model_name=None, context_cards=None,
              perimeter=None):
        # Invariant 11, at this backing's own layer (#424).
        if slug in self._meta:
            raise _SessionExists(f"session '{slug}' already exists", details={"slug": slug})
        meta = SessionMeta(session_id="mem-" + slug, slug=slug, created_at="t", updated_at="t",
                           provider=provider, model_name=model_name, context_cards=context_cards,
                           perimeter=perimeter)
        self._meta[slug] = meta
        self._req[slug] = request
        self._art[slug] = {}
        return meta

    def read_meta(self, slug):
        if slug not in self._meta:
            raise _NotFound(f"no session '{slug}'", details={"slug": slug})
        return self._meta[slug]

    def delete(self, slug):
        # The dict-backed analogue of the file backing's lock-then-remove (#238).
        if slug not in self._meta:
            raise _NotFound(f"no session '{slug}'", details={"slug": slug})
        with self.lock(slug):
            self._meta.pop(slug, None)
            self._model.pop(slug, None)
            self._req.pop(slug, None)
            self._art.pop(slug, None)
            for key in [k for k in self._revs if k[0] == slug]:
                self._revs.pop(key, None)

    def write_meta(self, slug, meta): self._meta[slug] = meta
    def list_slugs(self): return sorted(self._meta)

    def list_unexaminable(self):
        # `[]`, and it is a real answer rather than a stub (#80).
        return []

    def load_model(self, slug):
        if slug not in self._model:
            raise _NotFound(f"no model '{slug}'", details={"slug": slug})
        return self._model[slug]

    def load_revision(self, slug, revision):
        if (slug, revision) not in self._revs:
            raise _NotFound(f"no revision {revision}", details={"slug": slug, "revision": revision})
        return self._revs[(slug, revision)]

    def save_revision(self, slug, model, *, expected_revision=None, provenance=None):
        meta = self.read_meta(slug)
        if expected_revision is not None and meta.current_revision != expected_revision:
            raise _RevConflict("conflict", details={"expected": expected_revision,
                                                    "actual": meta.current_revision})
        rev = meta.current_revision + 1
        prov = dict(provenance or {})
        meta.revisions.append(RevisionRecord(
            revision=rev, created_at="t", previous_revision=meta.current_revision or None,
            model_hash="sha256:mem", provider=prov.get("provider"), model_name=prov.get("model_name"),
            surface=prov.get("surface"), prompt_version=prov.get("prompt_version")))
        meta.current_revision = rev
        self._model[slug] = model
        self._revs[(slug, rev)] = model
        return rev, meta

    def request_text(self, slug): return self._req.get(slug, "")
    def context_cards(self, slug): return self._meta[slug].context_cards if slug in self._meta else None

    def save_artifact(self, slug, artifact_type, filename, content, *, source_revision, stale=False):
        self._art[slug][filename] = content
        st = ArtifactStatus(revision=source_revision, filename=filename, updated_at="t", stale=stale)
        self._meta[slug].artifact_status[artifact_type] = st
        return st

    def load_artifact(self, slug, filename): return self._art.get(slug, {}).get(filename)


class TestInMemoryRepositoryConformance(SessionRepositoryConformance):
    """The non-file backing above, proven against the shared suite."""

    def make_repository(self):
        return InMemorySessionRepository()


class TestFileRepositoryConformance(SessionRepositoryConformance):
    """The shipped file backing, against the same shared suite."""

    @pytest.fixture(autouse=True)
    def _workspace(self, tmp_path):
        self._root = tmp_path

    def make_repository(self):
        return FileSessionRepository(root=self._root)


def test_session_service_runs_unchanged_on_a_non_file_repository():
    from requivo.services.sessions import SessionService
    repo = InMemorySessionRepository()
    assert isinstance(repo, SessionRepository)          # satisfies the protocol (runtime-checkable)
    svc = SessionService(repo)

    svc.create_session("a leave request", slug="leave-mem")
    r1 = svc.update_model("leave-mem", out({"workflow": slot(60, "inferred", "high")}).model_dump(),
                          provenance={"provider": "anthropic", "surface": "cli-discover"})
    assert r1.revision == 1

    # artifact tracking + dependency-graph staleness, entirely in memory
    ArtifactService(repo).save("leave-mem", "criteria", "# c", source_revision=1)   # criteria consumes workflow
    r2 = svc.update_model(
        "leave-mem", out({"workflow": {**slot(95, "explicit", "high"), "value": "a → b"}}).model_dump())
    assert r2.revision == 2
    assert ArtifactService(repo).list("leave-mem")["criteria"]["stale"] is True

    # optimistic locking is enforced by the backing, not the file layout
    with pytest.raises(_RevConflict):
        svc.update_model("leave-mem", out({"workflow": slot(95, "explicit", "high")}).model_dump(),
                         expected_revision=0)

    # the rich status projection needs no filesystem either
    st = svc.status("leave-mem")
    assert st["revision"] == 2 and "understanding" in st
    # provenance is recorded per revision on the non-file backing
    assert [rr.surface for rr in svc.meta("leave-mem").revisions] == ["cli-discover", None]


# ── the grounding judgment, through the service (#593) ────────────────────────────────────────────
# `decision: the-engine-writes-the-missing-card`.


class _Judge:
    """A `ReasoningProvider` that also answers grounding questions."""

    name = "judging-stub"

    def __init__(self, judgment=None):
        from requivo.core.contracts import ContextJudgment
        self.judgment = judgment or ContextJudgment(decision="none", reason="ordinary software")
        self.asked: list[list] = []

    def judge_context(self, request, *, cards):
        self.asked.append(cards)
        return self.judgment

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False,
                perimeter=None):
        return _full_model_out()

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        raise AssertionError("no generation in these tests")

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None, perimeter=None):
        return {"provider": self.name, "model_name": "stub-model", "prompt_version": "sha256:0"}


def _full_model_out():
    return out({"problem": slot(80, "explicit", "high")})


def _disco(provider):
    from requivo.services.discovery import DiscoveryService
    return DiscoveryService(provider)


def test_an_explicit_card_selection_is_not_second_guessed(workspace):
    """A `--context` is a human decision. Paying to re-examine it would either agree at cost or disagree with
    nothing the service is allowed to do about it."""
    judge = _Judge()
    grounding = _disco(judge).judge_grounding("a request", cards=["b2b-platform"])

    assert judge.asked == [], "the judgment was billed over a selection the user had already made"
    assert grounding.judgment is None
    assert "--context" in grounding.why_not


def test_a_provider_that_cannot_judge_reports_not_asked_rather_than_no_card_needed(workspace):
    """`ContextJudge` is a protocol a provider may simply not implement (#492)."""
    grounding = _disco(_FakeProvider()).judge_grounding("a request", cards=None)

    assert grounding.judgment is None, "a provider that cannot judge produced a verdict anyway"
    assert grounding.why_not, "not asked, and it did not say why"


def test_the_judgment_reaches_the_provider_with_one_line_per_installed_card(workspace):
    """The summaries are read in `core` and passed down, so the provider cannot answer about a different set
    of cards than the session would actually load."""
    from requivo.core.context import available_cards

    judge = _Judge()
    _disco(judge).judge_grounding("a request", cards=None)

    assert len(judge.asked) == 1
    assert [c.stem for c in judge.asked[0]] == sorted(available_cards())


def test_an_install_with_no_cards_is_not_judged_as_needing_none(workspace, monkeypatch):
    """`load_context` refuses this install outright a moment later."""
    from requivo.services import discovery as disco_mod

    monkeypatch.setattr(disco_mod, "card_summaries", list)
    judge = _Judge()
    grounding = _disco(judge).judge_grounding("a request", cards=None)

    assert judge.asked == [], "an install with nothing to judge against was still billed"
    assert grounding.judgment is None and "no context cards" in grounding.why_not


def test_a_narrowing_verdict_reclaims_under_the_narrowed_identity(workspace):
    """The selection is half a session's identity (invariant 11), so acting on `installed` cannot be an edit."""
    from requivo.core.contracts import ContextJudgment

    judge = _Judge(ContextJudgment(decision="installed", reason="finance",
                                   cards=["financial-reporting"]))
    disco = _disco(judge)
    meta, grounding, cards, _routing = disco.claim_and_ground("a billing request", cards=None, slug=None)

    assert cards == ["financial-reporting"]
    assert meta.context_cards == ["financial-reporting"], (
        "the session records a selection it was not created with")
    assert grounding.judgment.decision.value == "installed"
    assert len(SessionService().list_sessions()) == 1, "the widened claim was left behind"


def test_a_session_this_call_did_not_create_is_never_deleted_by_a_verdict(workspace):
    """`create_session_report`'s boolean is the whole authorisation for the delete."""
    from requivo.core.contracts import ContextJudgment

    svc = SessionService()
    first = svc.create_session("a billing request")

    judge = _Judge(ContextJudgment(decision="installed", reason="finance",
                                   cards=["financial-reporting"]))
    meta, _grounding, cards, _routing = _disco(judge).claim_and_ground("a billing request", cards=None, slug=None)

    assert meta.slug == first.slug, "an idempotent re-entry landed somewhere else"
    assert cards is None, "a session this call did not create was narrowed anyway"
    assert svc.exists(first.slug), "a session this call did not create was deleted"


def test_a_session_that_moved_off_revision_zero_during_the_judgment_is_left_alone(workspace):
    """`created` was true a call ago, and a call ago is long enough for a model to have landed."""
    from requivo.core.contracts import ContextJudgment

    svc = SessionService()

    class _WritesMidJudgment(_Judge):
        def judge_context(self, request, *, cards):
            # A concurrent writer, at the only moment that matters.
            svc.update_model(svc.list_sessions()[0].slug, _full_model())
            return super().judge_context(request, cards=cards)

    judge = _WritesMidJudgment(ContextJudgment(decision="installed", reason="finance",
                                              cards=["financial-reporting"]))
    meta, _grounding, cards, _routing = _disco(judge).claim_and_ground("a billing request", cards=None, slug=None)

    assert svc.exists(meta.slug), "a session with a model in it was deleted on a verdict"
    assert cards is None, "the narrowing went ahead over a session that had moved on"
    assert svc.list_sessions()[0].current_revision == 1, "the concurrent write was lost"
