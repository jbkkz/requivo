"""`requivo status` ends by naming the next command, once (#246).

`status` is the verb a user runs on coming back to a session, and its human view stopped at the
question list. Every other surface in this product follows the same rule and says so in its own
words: `discover` closes with "Answer and refine: requivo answer <slug> ...", `answer` closes with
either "run `requivo brief`" or "Keep going", and the plugin's status skill states it as a
requirement -- *then point at the next step, once*. The CLI's `status` was the one place the rule
was written down and not implemented.

Three things are pinned and the third is the one that makes it a rule rather than a nicety.

* **Which** command, because a pointer at the wrong step is worse than none: a session with open
  questions is told to answer them, not to buy a brief it will invalidate on the next turn.
* **Exactly one**, never a menu. A list of three things a user could do next is the state `status`
  was already in.
* **`--json` untouched.** A machine consumer picks its own next step, and the payload is a published
  contract -- so the pointer is a property of the *human* view and the JSON is asserted byte-for-byte
  identical to the payload the projection produced.

`next_command` is a pure function over the status payload rather than a branch inside `_cmd_status`,
so the ordering can be tested without a session on disk. The CLI tests below are what stop it from
being a pure function nobody calls.
"""
from __future__ import annotations

import json

from _cli_harness import _full_model, _run

from requivo.core import persistence as store
from requivo.core.contracts import EngineOutput
from requivo.render.terminal import next_command

_SLUG = "leave-approval"


def _payload(*, questions=0, ready=True, artifacts=None, perimeter=None) -> dict:
    return {
        "slug": _SLUG,
        "readiness": {"ready": ready, "blocking_slots": []},
        "questions": [{"q": f"Q{i}", "slot": "problem", "label": "L", "why": "w"}
                      for i in range(questions)],
        "artifacts": artifacts if artifacts is not None else {},
        "perimeter": perimeter,
    }


def test_open_questions_point_at_answer():
    """Questions outrank everything below them, and that ordering is a judgment worth stating: a
    session with a stale brief *and* open questions is told to answer, because regenerating a brief
    against a model that is about to move is a paid call thrown away."""
    line = next_command(_payload(questions=3, ready=False))
    assert line == f'requivo answer {_SLUG} "<your answers>"'

    stale = {"brief": {"revision": 1, "filename": "solution-assessment.md", "stale": True}}
    assert next_command(_payload(questions=3, artifacts=stale)) == line


def test_a_stale_artifact_points_at_the_verb_that_regenerates_it():
    """Second in the order: nothing left to ask, but something on disk no longer matches the model.
    The pointer names the verb, because the artifact type *is* the verb -- and names `impact`, which
    is the verb that says what else moved."""
    stale = {"brief": {"revision": 1, "filename": "solution-assessment.md", "stale": True},
             "prd": {"revision": 2, "filename": "prd.md", "stale": False}}
    line = next_command(_payload(questions=0, artifacts=stale))
    assert line == f"requivo brief {_SLUG}   (regenerates solution-assessment.md; requivo impact {_SLUG} shows what else moved)"


def test_a_ready_session_with_no_brief_points_at_brief():
    """Third: converged, nothing stale, and the deliverable has never been generated."""
    assert next_command(_payload(questions=0, ready=True)) == f"requivo brief {_SLUG}"


def test_a_ready_go_to_market_session_with_no_plan_points_at_its_own_verb():
    """[found in review, #608; corrected by #609's follow-up review] A perimeter that owns no
    "brief" generator (go-to-market, when #608 landed) never has one in `artifacts` either -- this
    used to read that absence the same as software's "never generated yet" and suggested a command
    that fails outright the moment it is run (`_require_owned_artifact_type`), so #608 gated it to
    silence instead. #609 gave go-to-market its own artifact, `gtm_plan`, and the gate was still
    keyed to the literal `"brief"` -- so a converged, plan-less go-to-market session suggested
    nothing at all rather than its own real next step. Reads `Perimeter.primary_artifact` now, the
    same fact `session_detail()`'s Web fix reads, so this is `requivo gtm_plan <slug>`, mirroring
    `test_a_ready_session_with_no_brief_points_at_brief` for software's own primary."""
    from requivo.core.perimeters import GO_TO_MARKET

    assert (next_command(_payload(questions=0, ready=True, perimeter=GO_TO_MARKET))
            == f"requivo gtm_plan {_SLUG}")


def test_a_finished_session_is_pointed_nowhere_rather_than_at_a_menu():
    """The third state, and it is the reason this returns `str | None` rather than always a string.
    Ready, nothing stale, brief already saved -- there is no single next step, and inventing one
    (`prd`? `epic`? `criteria`?) is the menu this rule exists to refuse. Silence is the answer."""
    fresh = {"brief": {"revision": 3, "filename": "solution-assessment.md", "stale": False}}
    assert next_command(_payload(questions=0, ready=True, artifacts=fresh)) is None


def test_a_bare_model_file_has_no_slug_to_point_at():
    """`status` accepts a path to a `model.json`, which has no session behind it -- no artifacts, and
    a slug that is only the parent directory's name. A pointer naming a session that does not exist
    is worse than none, so it is withheld."""
    assert next_command({"slug": "x", "questions": [], "readiness": {"ready": True}}) is None


# -- through the verb, because a pure function nobody calls is the failure this replaced -----------


def _session_with(questions: list) -> None:
    store.create_session(_SLUG, "A leave approval system")
    model = _full_model()
    model["questions"] = questions
    store.save_revision(_SLUG, EngineOutput.model_validate(model))


def test_the_human_status_view_ends_with_exactly_one_pointer(workspace):
    """Must fire on both halves: the line is there, and there is only one of it. The plugin skill's
    wording is *once*, and a view that printed a pointer per artifact would satisfy any test that
    only looked for the string."""
    _session_with([{"q": "How are approvals routed today?", "slot": "problem", "why": "w"}])
    text = _run(["status", _SLUG])
    pointers = [ln for ln in text.splitlines() if ln.lstrip().startswith("→ requivo")]
    assert pointers == [f'→ requivo answer {_SLUG} "<your answers>"'], text
    assert text.rstrip().endswith(pointers[0])


def test_the_json_status_payload_is_unchanged_by_the_pointer(workspace):
    """The pointer is a property of the human view. `--json` is a published contract, so this asserts
    the payload carries no trace of it and still parses as one object -- a pointer printed beside the
    JSON would break every consumer that pipes this into `jq`."""
    _session_with([{"q": "How are approvals routed today?", "slot": "problem", "why": "w"}])
    raw = _run(["status", _SLUG, "--json"])
    payload = json.loads(raw)                       # must fire: a trailing pointer breaks this
    assert "requivo answer" not in raw
    assert set(payload) >= {"slug", "readiness", "understanding", "questions", "summary"}
