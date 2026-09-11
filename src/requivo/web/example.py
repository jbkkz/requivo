"""The bundled example session — Web's keyless activation path (#226).

Web is the declared product experience, and until this existed a keyless first run showed an empty
page and a provider notice: a product surface with nothing on it. The CLI answered the same problem
with `requivo demo` and said why in `_cmd_demo`'s own comment — *a visitor shouldn't need a key, a
clone, and a venv before feeling what the product does.*

Three constraints shape everything below, and each of them is a decision rather than an
implementation detail:

* **It goes through the ordinary validated path.** `create_session` + `update_model`, not a
  hand-written directory. The example is then a real session in every sense a reader can check: it
  has a revision, a frozen copy in `revisions/`, a readiness verdict computed by the same code as
  everyone else's, and it opens in the CLI and in Claude Code. A directory assembled here would be
  a *replica* of a session, and the first thing a replica does is drift.
* **Nothing is reasoned.** No provider, no network, no key. The payload shipped in the wheel
  (`src/requivo/assets/demo/`) is the output of a real run, replayed.
* **It says what it is.** A sample session the reader did not create, appearing in a list of
  sessions they own, has to be recognisable as an example wherever it renders.

`is_example` answers that last one from the **request text**, not from the slug. A slug test is the
obvious implementation and it is wrong in both directions: a workspace already holding a session
called `example-event-check-in` pushes the sample to a derived name (`create_session` claims a slug
atomically and falls back — invariant 11), so the sample would lose its badge and the squatter gain
one. The request is what a session *is*; the slug is a name it happened to land under. Pinned by
`test_the_example_is_recognised_by_what_it_asks_not_by_the_name_it_landed_under`.
"""

from __future__ import annotations

import json

from requivo.core.errors import RevisionConflictError
from requivo.paths import DEMO
from requivo.services.artifacts import ArtifactService
from requivo.services.sessions import SessionService

# The name the sample lands under when nothing else holds it. `create_session` may hand back a
# derived name instead (invariant 11), which is why nothing downstream reads this to decide what
# the sample *is* — see `is_example`.
#
# Deliberately not `cli.DEMO_SLUG` ("event-checkin-reconciliation"), which names the browsable copy
# under `examples/` and travels in a URL. This one names a directory in the reader's own workspace
# and shows up in `requivo session list`, where there is no badge and the name is the only signal —
# so it says what it is. The two are the same payload and the docs say so; sharing one constant
# would make a slug in somebody's workspace hostage to a directory rename in this repository.
EXAMPLE_SLUG = "example-event-check-in"

# Recorded on the revision this seeds. `provider` and `model_name` stay absent, deliberately:
# invariant 6 says provenance is real or absent, and nothing reasoned this apply. The payload was
# produced by a real Anthropic run months ago; claiming that provider *here* would describe a call
# this process did not make. `session rescope` records `surface` alone for the same reason.
# Pinned by `test_the_revision_claims_no_provider_it_did_not_use`.
EXAMPLE_SURFACE = "web-example"


def _read(name: str) -> str:
    """One bundled asset, UTF-8 on purpose (invariant 16 — the locale default is not a codec)."""
    return (DEMO / name).read_text(encoding="utf-8")


def _unquote(markdown: str) -> str:
    """The client email out of the payload's markdown wrapper.

    `assets/demo/request.md` is written for `requivo demo` to *narrate*: a heading, two lines
    explaining what kind of input this is, and then the email itself as a blockquote. A session
    captures what the client said, so the wrapper would be a description of the request standing
    where the request goes — and the understanding on screen would then be read against a paragraph
    about the example rather than against the example.

    Falls back to the whole text when there is no blockquote, so a future payload written without
    one still seeds something rather than an empty request (which `create_session` refuses).
    """
    quoted = [line.lstrip()[1:].strip() for line in markdown.splitlines()
              if line.lstrip().startswith(">")]
    return "\n".join(quoted).strip() or markdown.strip()


def example_request() -> str:
    """The request the example session captures."""
    return _unquote(_read("request.md"))


def example_proposal() -> dict:
    """The bundled model, as the proposal `update_model` validates and applies.

    Parsed on each call rather than cached: `update_model` is handed this dict, and a cached one
    would be a shared mutable the next caller sees whatever the last one did to it.
    """
    return json.loads(_read("model.json"))


def example_brief() -> str:
    """The bundled decision brief, as the markdown `ArtifactService.save` records -- the real
    `brief_markdown()` rendering (not the ```text-fenced terminal capture `requivo demo` narrates),
    produced once offline from the same run `model.json` carries, so the file on disk is what that
    function actually produces rather than a paraphrase of it. Read as a file rather than
    reconstructed at seed time, like `example_request()`/`example_proposal()` (#429). Pinned by
    `test_the_bundled_brief_is_read_rather_than_restated`."""
    return _read("brief.md")


def _normalised(text: str) -> str:
    """Whitespace-insensitive form, for comparing a request read back off disk against the packaged
    one. Line endings differ between the platforms this runs on, and a sample that stopped being
    recognised as the example on Windows alone is exactly the class of bug this repo keeps finding
    one CI matrix at a time."""
    return " ".join(text.split())


def is_example(request_text: str) -> bool:
    """Whether a session is the bundled example — decided by what it asks. See the module docstring
    for why this is not a slug test."""
    return bool(request_text) and _normalised(request_text) == _normalised(example_request())


def seed_example(sessions: SessionService, artifacts: ArtifactService | None = None) -> str:
    """Materialise the bundled example as a real local session. Returns the slug it landed under.

    **Clicking twice navigates rather than refusing.** `create_session` is idempotent on identity
    (the request *and* its context-card selection), so the second call hands back the session the
    first one made — the reader ends up looking at the example either way, which is what they asked
    for. Refusing was the other honest shape and it teaches nothing: the button says *example*, and
    a reader who presses it again wants the example.

    The model is applied only at revision 0. Re-applying an identical model would mint a second
    revision with the same content and a provenance record describing an event that did not happen,
    and it would overwrite a session the reader had since refined with a key of their own. Pinned by
    `test_a_second_click_returns_to_the_same_session_rather_than_making_another`.

    **`expected_revision` is what makes that a guarantee rather than a sequential accident.** The
    `current_revision` read is outside any lock, and this route is a plain `def`, which Starlette
    runs on a worker thread — so two first clicks arriving together (a double submit before the 303
    lands, two tabs) can both see revision 0 through `create_session`'s idempotent-identity return
    and both go on to apply. The precondition is what turns the loser of that race into a no-op
    instead of a spurious revision 2 of identical content: exactly invariant 9's rule that a check
    not held across the write it authorises is not a check. The conflict is *swallowed* rather than
    raised, uniquely here, and only because of what it means at this one call site: somebody else
    just seeded the very session this call was going to seed, which is the outcome asked for. Every
    other caller of `update_model` must let a `RevisionConflictError` reach its reader, because
    there the other writer applied something *different*.

    No existence check precedes the create: `create_session` is the atomic claim (invariant 11), and
    a check here would be the preceding-existence-check that invariant exists to refuse.

    **The decision brief is seeded the same way, through the same call** (#429). README.md promises
    the click delivers "the understanding, the open questions, the readiness verdict and the decision
    brief" -- a bundled `model.json` alone only ever produced the first three; `artifacts/list.html`
    showed "Nothing generated yet" and `GET .../artifacts/brief` 404d. `example_brief()` is bundled
    the same way `example_proposal()` is, and saved through the same validated `ArtifactService.save`
    path every other artifact goes through -- so it carries a revision-1 freshness the dependency
    graph computed rather than an asserted one, exactly like the model seed's invariant-6 treatment
    leaves `provider`/`model_name` absent rather than false.

    Gated on `"brief" not in artifacts.list(...)` rather than on `meta.current_revision == 0`
    alongside the model seed above: the two guards answer different questions. The model must never
    be re-applied once the reader has refined it (that would mint a revision describing an event that
    did not happen). The brief must never be re-seeded once *anything* -- the seed, or the reader's
    own real generation with their own key -- has recorded one, seeded or real, so a click after a
    real generation cannot silently discard it. Checking presence directly is what makes a re-seed
    idempotent in the ordinary case (#429's acceptance criterion) without either question leaking
    into the other's answer.

    **The check and the save run under the same lock, and that is not incidental** (invariant 9: a
    precondition is only a check when it is held across the write it authorises). `artifacts.list()`
    is an unlocked read and `artifacts.save()` only locks around itself, so a plain
    check-then-act left a window: a reader's own real generation (`POST
    .../artifacts/brief`, which reaches `ArtifactService.save` too) completing inside that window
    would have its content silently overwritten by this call's unconditional save — the exact thing
    the paragraph above says must never happen. `sessions.repo.lock` is the same lock
    `ArtifactService.save` takes internally (`services/artifacts.py`), and it is per-thread
    re-entrant (invariant 9's own note), so holding it here across both the read and the write
    serialises this whole gate against any other caller's `save` for this slug, including a real
    generation, without deadlocking against the nested call this function makes into its own `save`.
    """
    artifacts = artifacts if artifacts is not None else ArtifactService(repo=sessions.repo)
    meta = sessions.create_session(example_request(), slug=EXAMPLE_SLUG)
    if meta.current_revision == 0:
        try:
            sessions.update_model(meta.slug, example_proposal(), expected_revision=0,
                                  provenance={"surface": EXAMPLE_SURFACE})
        except RevisionConflictError:
            pass  # a concurrent click seeded it first — see the docstring
    with sessions.repo.lock(meta.slug):
        if "brief" not in artifacts.list(meta.slug):
            artifacts.save(meta.slug, "brief", example_brief(), source_revision=1)
    return meta.slug
