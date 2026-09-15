"""The `ReasoningProvider` seam is not Anthropic-shaped (a fake provider drives a whole discovery,
and a revision's provenance is enough to reproduce the call that produced it), a generation carries
the revision it read even when a concurrent write lands mid-flight (invariant 2), and
`SessionService`'s orchestration runs unchanged on a non-file `SessionRepository` -- the storage
seam's own conformance suite, plus the one integration test that is specific to this repo's services
rather than to the seam's contract (#424). Split by #555 from `test_sessions.py`.
"""
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
    # A turn has the same seam as a generation, so a caller that passes no expectation still gets one:
    # the revision the turn actually read. Without it, the CLI's `answer` would quietly overwrite a
    # change made in a browser tab between the read and the apply.
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
    """The full user journey the tri-state exists for: discovery → assessment → an ordinary answer.
`engine.md` asks a turn for model/questions/summary only, so a refinement reply carries no
decisions — and this whole path (provider parse → apply → diff → freshness) used to read that as
a deletion, wiping the reasoning the assessment had just established while reporting no change
and leaving the PRD marked fresh."""
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
    """Implements every member `ReasoningProvider` *declares* — and nothing more.

    The stand-in for the second implementation the seam exists for. `name` is read by the very first
    thing a discovery does, so an object without one is not a provider; whether anything can *tell*
    is what this pins."""

    def analyze(self, request, *, current_model=None, answers=None, only=None):
        raise AssertionError("a provider missing `name` must fail before it is asked to reason")

    def generate(self, artifact_type, model, *, only=None):
        raise AssertionError("not reached")

    def model_name(self):
        return "nameless-1"

    def provenance(self, op, *, only=None):
        return {"provider": "nameless", "model_name": self.model_name(), "prompt_version": "sha256:x"}


def test_discovery_runs_on_a_provider_that_is_not_anthropic(workspace):
    from requivo.services.discovery import DiscoveryService

    slug = DiscoveryService(_FakeProvider()).start("A leave approval system.", slug="fake-prov")
    meta = SessionService().meta(slug)
    # Nothing hard-codes "anthropic": the session and its revision are stamped by the provider itself.
    assert meta.provider == "fake" and meta.model_name == "fake-model-1"
    assert [(r.provider, r.model_name) for r in meta.revisions] == [("fake", "fake-model-1")]


def test_the_provider_protocol_declares_every_member_the_orchestration_reads(workspace):
    """`provider.name` is read on the first discovery, so it is part of the contract or the contract
    is not the contract. `@runtime_checkable` does check a bare data annotation — only `issubclass`
    is refused for a protocol with non-method members — so declaring it is enforcement, not comment."""
    from requivo.providers.anthropic import AnthropicProvider
    from requivo.providers.base import ReasoningProvider
    from requivo.services.discovery import DiscoveryService

    # Must-fire half: real conformers satisfy the protocol. Without it, a protocol that rejected
    # everything — or one that stopped being runtime-checkable — would pass the assertion below.
    assert isinstance(_FakeProvider(), ReasoningProvider)
    assert isinstance(AnthropicProvider.__new__(AnthropicProvider), ReasoningProvider)  # no API key needed

    # Must-not-fire half: everything declared, `name` absent.
    assert not isinstance(_NamelessProvider(), ReasoningProvider)

    # …and the positive control that keeps the line above from being about a decorative member:
    # `name` is what the orchestration actually reaches for, before it reasons.
    with pytest.raises(AttributeError, match="name"):
        DiscoveryService(_NamelessProvider()).start("A leave approval system.", slug="nameless")


def test_a_revision_records_the_prompt_it_was_reasoned_against(workspace):
    """Invariant 6: provenance is real or absent. Each revision records provider, model, surface and a
hash of the exact prompt it was reasoned against; a provenance field nothing populates is worse
than none, because a reader trusts a column that is always filled. The deterministic side of the
same rule — an apply that made no call carries no usage — is
`test_a_deterministic_apply_carries_no_usage_provenance` (moved here from CLAUDE.md by #286)."""
    # A revision log that is only "anthropic, at 14:02" cannot reproduce anything: behaviour here is
    # tuned by editing prompts and context cards, so the prompt identity is half the provenance.
    from requivo.providers.anthropic import prompt_version
    from requivo.services.discovery import DiscoveryService

    reply = {**_full_model(), "summary": {"objective": "A leave approval system"}}
    slug = DiscoveryService(client=_RacingClient(json.dumps(reply), lambda: None)).start(
        "A leave approval system.", slug="prov")

    rec = SessionService().meta(slug).revisions[-1]
    assert rec.provider == "anthropic" and rec.model_name
    assert rec.prompt_version and rec.prompt_version.startswith("sha256:")
    # It follows the context-card selection, because a different card set is different reasoning.
    # A *real* card rather than `only=[]`: an empty selection is now refused (#13), because a
    # selection that selects nothing renders exactly like a clean load of everything.
    assert prompt_version("analyze") != prompt_version("analyze", only=["b2b-platform"])


# ── SessionRepository: the storage seam (proves the service is backing-agnostic, #424) ──


class InMemorySessionRepository:
    """A dict-backed SessionRepository — no filesystem, no `.requivo/` directory. A faithful stand-in
    for a Postgres backing (everything is mutation-backed, so has_meta == exists, ensure_writable is a
    no-op check)."""

    def __init__(self):
        self._meta: dict = {}
        self._model: dict = {}
        self._revs: dict = {}      # (slug, revision) → model, the history a file backing keeps on disk
        self._req: dict = {}
        self._art: dict = {}
        self._locks: dict = {}                 # slug -> threading.RLock, created on first use
        self._locks_guard = threading.Lock()    # protects _locks itself, not any session's data

    def _lock_for(self, slug):
        # threading.RLock is re-entrant *per thread* by construction -- the same primitive this
        # backing needs for invariant 9's two halves at once (mutual exclusion across threads,
        # re-entrancy within one). A bare `yield` here used to be the whole implementation, on the
        # reasoning that a single dict mutation needs no lock -- true, and beside the point: the
        # *caller* (SessionService) takes this lock around several such mutations and depends on
        # nothing else interleaving with the whole sequence, which a no-op cannot provide. Found by
        # `requivo.testing.repository_conformance.SessionRepositoryConformance` (#424), which this
        # fake did not pass before this fix.
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

    def create(self, slug, request, *, provider=None, model_name=None, context_cards=None):
        # Invariant 11, at this backing's own layer: the claim is a dict-key collision check rather
        # than a rename, but it owes the same answer -- a second create() on a slug already in the
        # store must not silently overwrite it. Added for the conformance suite (#424); before it
        # this method had no such check and lost the first session's identity on a collision, the
        # exact bug invariant 11 documents for the file backing's pre-fix `has_meta`-then-create.
        if slug in self._meta:
            raise _SessionExists(f"session '{slug}' already exists", details={"slug": slug})
        meta = SessionMeta(session_id="mem-" + slug, slug=slug, created_at="t", updated_at="t",
                           provider=provider, model_name=model_name, context_cards=context_cards)
        self._meta[slug] = meta
        self._req[slug] = request
        self._art[slug] = {}
        return meta

    def read_meta(self, slug):
        if slug not in self._meta:
            raise _NotFound(f"no session '{slug}'", details={"slug": slug})
        return self._meta[slug]

    def delete(self, slug):
        # The dict-backed analogue of the file backing's lock-then-remove: a Postgres row delete has
        # no lock file to unlink, but it owes the identical release-the-slug guarantee the conformance
        # suite checks (#238) -- a stale key left in any of these dicts would make a later create()
        # for the same slug collide with residue this session left behind.
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
        # `[]`, and it is a real answer rather than a stub: a dict key either is a session or is not
        # there, so the question this method exists for cannot arise on this backing (#80). What
        # would be wrong is dropping a row that *was* enumerated and could not be decoded — see the
        # protocol's docstring; there are none here to drop.
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
    """The non-file backing above, proven against the shared suite -- the factory wiring #424's
    acceptance criteria ask this module to shrink to."""

    def make_repository(self):
        return InMemorySessionRepository()


class TestFileRepositoryConformance(SessionRepositoryConformance):
    """The shipped file backing, against the same shared suite -- both implementations this repo
    carries run identically against `SessionRepositoryConformance`, which is the point: a third
    implementation (Postgres, out of this repo) has the same bar to clear."""

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
# `decision: the-engine-writes-the-missing-card`. These drive `DiscoveryService` over stubs, because
# what they pin is which of four states the service reports and whether it is allowed to delete a
# session -- neither is visible from the provider function alone.


class _Judge:
    """A `ReasoningProvider` that also answers grounding questions. Records what it was asked."""

    name = "judging-stub"

    def __init__(self, judgment=None):
        from requivo.core.contracts import ContextJudgment
        self.judgment = judgment or ContextJudgment(decision="none", reason="ordinary software")
        self.asked: list[list] = []

    def judge_context(self, request, *, cards):
        self.asked.append(cards)
        return self.judgment

    def analyze(self, request, *, current_model=None, answers=None, only=None, reuse_system=False):
        return _full_model_out()

    def generate(self, artifact_type, model, *, only=None, **kwargs):
        raise AssertionError("no generation in these tests")

    def model_name(self):
        return "stub-model"

    def provenance(self, op, *, only=None):
        return {"provider": self.name, "model_name": "stub-model", "prompt_version": "sha256:0"}


def _full_model_out():
    return out({"problem": slot(80, "explicit", "high")})


def _disco(provider):
    from requivo.services.discovery import DiscoveryService
    return DiscoveryService(provider)


def test_an_explicit_card_selection_is_not_second_guessed(workspace):
    """A `--context` is a human decision. Paying to re-examine it would either agree at cost or
    disagree with nothing the service is allowed to do about it."""
    judge = _Judge()
    grounding = _disco(judge).judge_grounding("a request", cards=["b2b-platform"])

    assert judge.asked == [], "the judgment was billed over a selection the user had already made"
    assert grounding.judgment is None
    assert "--context" in grounding.why_not


def test_a_provider_that_cannot_judge_reports_not_asked_rather_than_no_card_needed(workspace):
    """`ContextJudge` is a protocol a provider may simply not implement. *Nobody looked* and *no card
    is needed* must never be the same answer -- that is the silent verdict #492 refused a status for."""
    grounding = _disco(_FakeProvider()).judge_grounding("a request", cards=None)

    assert grounding.judgment is None, "a provider that cannot judge produced a verdict anyway"
    assert grounding.why_not, "not asked, and it did not say why"


def test_the_judgment_reaches_the_provider_with_one_line_per_installed_card(workspace):
    """The summaries are read in `core` and passed down, so the provider cannot answer about a
    different set of cards than the session would actually load."""
    from requivo.core.context import available_cards

    judge = _Judge()
    _disco(judge).judge_grounding("a request", cards=None)

    assert len(judge.asked) == 1
    assert [c.stem for c in judge.asked[0]] == sorted(available_cards())


def test_an_install_with_no_cards_is_not_judged_as_needing_none(workspace, monkeypatch):
    """`load_context` refuses this install outright a moment later, with a remedy this method has no
    better version of. Answering "no card is needed" here would be the one wrong answer."""
    from requivo.services import discovery as disco_mod

    monkeypatch.setattr(disco_mod, "card_summaries", list)
    judge = _Judge()
    grounding = _disco(judge).judge_grounding("a request", cards=None)

    assert judge.asked == [], "an install with nothing to judge against was still billed"
    assert grounding.judgment is None and "no context cards" in grounding.why_not


def test_a_narrowing_verdict_reclaims_under_the_narrowed_identity(workspace):
    """The selection is half a session's identity (invariant 11), so acting on `installed` cannot be
    an edit -- the empty session this call just made is deleted and re-claimed under the narrower
    identity, leaving exactly one session that records the cards it will actually reason against."""
    from requivo.core.contracts import ContextJudgment

    judge = _Judge(ContextJudgment(decision="installed", reason="finance",
                                   cards=["financial-reporting"]))
    disco = _disco(judge)
    meta, grounding, cards = disco.claim_and_ground("a billing request", cards=None, slug=None)

    assert cards == ["financial-reporting"]
    assert meta.context_cards == ["financial-reporting"], (
        "the session records a selection it was not created with")
    assert grounding.judgment.decision.value == "installed"
    assert len(SessionService().list_sessions()) == 1, "the widened claim was left behind"


def test_a_session_this_call_did_not_create_is_never_deleted_by_a_verdict(workspace):
    """`create_session_report`'s boolean is the whole authorisation for the delete. Re-entering an
    existing session idempotently and then deleting it on a verdict would destroy somebody's claim
    on the strength of a judgment about a request they never re-ran."""
    from requivo.core.contracts import ContextJudgment

    svc = SessionService()
    first = svc.create_session("a billing request")

    judge = _Judge(ContextJudgment(decision="installed", reason="finance",
                                   cards=["financial-reporting"]))
    meta, _grounding, cards = _disco(judge).claim_and_ground("a billing request", cards=None, slug=None)

    assert meta.slug == first.slug, "an idempotent re-entry landed somewhere else"
    assert cards is None, "a session this call did not create was narrowed anyway"
    assert svc.exists(first.slug), "a session this call did not create was deleted"


def test_a_session_that_moved_off_revision_zero_during_the_judgment_is_left_alone(workspace):
    """`created` was true a call ago, and a call ago is long enough for a model to have landed. The
    authorisation is re-read under the lock (invariant 9), because a stale one is how a delete stops
    being safe."""
    from requivo.core.contracts import ContextJudgment

    svc = SessionService()

    class _WritesMidJudgment(_Judge):
        def judge_context(self, request, *, cards):
            # A concurrent writer, at the only moment that matters: after the claim, before the
            # verdict is acted on.
            svc.update_model(svc.list_sessions()[0].slug, _full_model())
            return super().judge_context(request, cards=cards)

    judge = _WritesMidJudgment(ContextJudgment(decision="installed", reason="finance",
                                              cards=["financial-reporting"]))
    meta, _grounding, cards = _disco(judge).claim_and_ground("a billing request", cards=None, slug=None)

    assert svc.exists(meta.slug), "a session with a model in it was deleted on a verdict"
    assert cards is None, "the narrowing went ahead over a session that had moved on"
    assert svc.list_sessions()[0].current_revision == 1, "the concurrent write was lost"


# ── the uncovered verdict writes a card and reclaims under it alone (#598) ─────────────────────────


def _generated_card(**overrides):
    from requivo.core.contracts import GeneratedCard

    base = dict(stem="dentistry", business_domain="dentistry", product_type="one_shot",
               typical_users=["front desk"], what_it_does="tracks patient appointments",
               entities=["patient", "appointment"], domain_concepts=["co-pay"],
               regulatory="HIPAA", technical_constraints="", traps=[], configurability="")
    base.update(overrides)
    return GeneratedCard.model_validate(base)


class _CardWritingJudge(_Judge):
    """A `_Judge` that also implements `CardWriter` -- the stand-in for a provider that can both
    judge and write (#598). `card=None` leaves `write_card` unimplemented via a plain `_Judge`
    instead, which is what `isinstance(provider, CardWriter)` is testing."""

    def __init__(self, judgment=None, card=None):
        super().__init__(judgment)
        self.card = card
        self.write_calls: list[str] = []

    def write_card(self, request):
        self.write_calls.append(request)
        return self.card


def test_an_uncovered_verdict_writes_a_card_and_reclaims_under_it_alone(workspace):
    """The `uncovered` mirror of the `installed` reclaim above: the empty session this call just
    made is deleted and re-claimed selecting the card just written, alone -- the other cards are
    irrelevant, and removing them is the entire point of the lot."""
    from requivo.core.contracts import ContextJudgment

    card = _generated_card()
    judge = _CardWritingJudge(ContextJudgment(decision="uncovered", reason="dentistry has "
                                              "licensing rules"), card=card)
    meta, grounding, cards = _disco(judge).claim_and_ground(
        "a dental clinic scheduler", cards=None, slug=None)

    assert cards == ["dentistry"]
    assert meta.context_cards == ["dentistry"]
    assert grounding.written_card == card
    assert len(SessionService().list_sessions()) == 1, "the widened claim was left behind"
    assert (workspace / ".requivo" / "context" / "dentistry.md").exists()


def test_a_provider_that_cannot_write_a_card_falls_back_to_the_report_only_warning(workspace):
    """`CardWriter` is a protocol a provider may simply not implement -- the `uncovered` mirror of
    `test_a_provider_that_cannot_judge_reports_not_asked_rather_than_no_card_needed`."""
    from requivo.core.contracts import ContextJudgment

    judge = _Judge(ContextJudgment(decision="uncovered", reason="dentistry has licensing rules"))
    meta, grounding, cards = _disco(judge).claim_and_ground(
        "a dental clinic scheduler", cards=None, slug=None)

    assert cards is None, "a provider that cannot write a card narrowed the selection anyway"
    assert grounding.written_card is None
    assert not (workspace / ".requivo" / "context").exists()


def test_a_colliding_stem_falls_back_to_the_report_only_warning(workspace):
    """`write_generated_card` refuses rather than shadows (invariant 3); the service must fall back
    to the report-only warning rather than raise the discovery itself, the same way a provider that
    cannot judge falls back rather than aborting."""
    from requivo.core.contracts import ContextJudgment

    existing = workspace / ".requivo" / "context"
    existing.mkdir(parents=True)
    (existing / "dentistry.md").write_text("ORIGINAL", encoding="utf-8")

    judge = _CardWritingJudge(ContextJudgment(decision="uncovered", reason="dentistry has "
                                              "licensing rules"), card=_generated_card())
    meta, grounding, cards = _disco(judge).claim_and_ground(
        "a dental clinic scheduler", cards=None, slug=None)

    assert cards is None, "a colliding stem narrowed the selection anyway"
    assert grounding.written_card is None
    assert (existing / "dentistry.md").read_text(encoding="utf-8") == "ORIGINAL"


def test_a_session_this_call_did_not_create_is_never_written_a_card_by_an_uncovered_verdict(workspace):
    """The `uncovered` mirror of the `installed` non-creator test: an idempotent re-entry onto
    somebody else's session must not spend the card-writing call at all, let alone act on it."""
    from requivo.core.contracts import ContextJudgment

    svc = SessionService()
    first = svc.create_session("a dental clinic scheduler")

    judge = _CardWritingJudge(ContextJudgment(decision="uncovered", reason="dentistry has "
                                              "licensing rules"), card=_generated_card())
    meta, _grounding, cards = _disco(judge).claim_and_ground(
        "a dental clinic scheduler", cards=None, slug=None)

    assert meta.slug == first.slug, "an idempotent re-entry landed somewhere else"
    assert cards is None, "a session this call did not create was narrowed anyway"
    assert judge.write_calls == [], "the card-writing call was billed for a verdict it cannot act on"


def test_a_session_that_moved_off_revision_zero_during_the_card_write_is_left_alone(workspace):
    """The `uncovered` mirror of the judgment-race test above, at its own paid call: the
    authorisation is re-read under the lock, and a stale one does not delete a session with a model
    in it. The card is left on disk regardless -- it is reusable by a later session either way, and
    what is unsafe is only the *delete*, not the write (invariant 9)."""
    from requivo.core.contracts import ContextJudgment

    svc = SessionService()

    class _WritesMidCardWrite(_CardWritingJudge):
        def write_card(self, request):
            svc.update_model(svc.list_sessions()[0].slug, _full_model())
            return super().write_card(request)

    judge = _WritesMidCardWrite(ContextJudgment(decision="uncovered", reason="dentistry has "
                                                "licensing rules"), card=_generated_card())
    meta, _grounding, cards = _disco(judge).claim_and_ground(
        "a dental clinic scheduler", cards=None, slug=None)

    assert svc.exists(meta.slug), "a session with a model in it was deleted on a verdict"
    assert cards is None, "the narrowing went ahead over a session that had moved on"
    assert svc.list_sessions()[0].current_revision == 1, "the concurrent write was lost"
    assert (workspace / ".requivo" / "context" / "dentistry.md").exists(), (
        "the card is reusable later even though this session did not select it")
