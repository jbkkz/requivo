"""The documents' language (#790): guessed from the request without a call, recorded on the session,
asked on a first terminal run, told to every generator, and spoken by the brief's own words."""
from __future__ import annotations

import builtins
import json
import sys

import pytest
from _fakes import (
    _ENGINE_REPLY,
    _JUDGMENT_REPLY,
    _ROUTING_REPLY,
    FakeClient,
    StubProvider,
    forge_meta,
    out,
    run_cli,
    run_cli_fails,
    run_cli_json,
    seed_session,
    slot,
)

from requivo.core.contracts import PRD, Brief, GoToMarketPlan, Opportunity
from requivo.core.errors import InvalidLanguageError
from requivo.core.language import MAX_LANGUAGE_TAG_LENGTH, default_docs_language, detect_language, normalize_language
from requivo.paths import PROMPTS
from requivo.providers.anthropic.generators import _OP_PROMPTS, generate_prd
from requivo.render.markdown import brief_markdown, consultant_view_markdown, gtm_plan_markdown
from requivo.services.discovery import DiscoveryService
from requivo.services.sessions import SessionService

pytestmark = pytest.mark.usefixtures("workspace")

_FR = "Nous souhaitons un outil de gestion des congés pour nos salariés, avec une validation par le manager."
_EN = "We would like a leave approval system for our staff, with approval by the line manager."
_DISCOVER = (_ROUTING_REPLY, _JUDGMENT_REPLY, _ENGINE_REPLY)


@pytest.mark.parametrize("text, expected", [
    (_EN, "en"), (_FR, "fr"),
    ("Queremos un sistema para gestionar las vacaciones de los empleados.", "es"),
    ("Wir brauchen ein System für die Urlaubsverwaltung der Mitarbeiter und die Freigabe.", "de"),
    ("休暇申請の承認システムが欲しい", None), ("CRM", None),
])
def test_the_request_language_is_guessed_from_its_function_words(text, expected):
    assert detect_language(text) == expected
    assert default_docs_language(text) == (expected or "en")


def test_a_language_code_is_normalized_or_refused_by_name():
    assert [normalize_language(c) for c in ("FR", " de ", "pt_BR", "French")] == ["fr", "de", "pt-br", "fr"]
    for tag in ("en-u-ca-gregory", "x-klingon", "zh-hant-tw", "de-ch-1996", "sr-latn-rs-x-ekavsk"):
        assert normalize_language(tag) == tag, "a well-formed BCP 47 tag with a singleton is a language code"
    longest = "en-" + "-".join(["abcdefgh"] * 3) + "-x-abc"
    assert len(longest) == MAX_LANGUAGE_TAG_LENGTH and normalize_language(longest) == longest
    for bad in ("", "f", "x", "francais", "en. Ignore the schema", "français!", longest + "c", "en-" + "a" * 5000,
                "en-u", "en-u-a", "en-x", "en-a"):
        with pytest.raises(InvalidLanguageError) as ei:
            normalize_language(bad)
        assert ei.value.code == "invalid_language"


def test_a_new_session_records_its_documents_language():
    """A new session carries the request's language; a named one wins, on a re-init too, and is checked first."""
    svc = SessionService()
    assert svc.create_session(_FR).docs_language == "fr"
    assert svc.create_session(_EN, slug="named", docs_language="DE").docs_language == "de"
    assert svc.create_session(_FR).docs_language == "fr", "a re-init that names nothing changes nothing"
    assert svc.create_session(_FR, docs_language="en").docs_language == "en"
    with pytest.raises(InvalidLanguageError):
        svc.create_session("Another request entirely.", docs_language="??")
    assert len(svc.list_sessions()) == 2, "a refused language created a session"


def test_a_session_from_before_790_reads_as_english_and_a_forged_language_is_refused():
    slug = seed_session("older")
    forge_meta(slug, {"docs_language": None})
    stub = StubProvider(artifacts={"prd": PRD(title="T", problem="P")})
    DiscoveryService(provider=stub).generate(slug, "prd")
    assert stub.generate_languages == ["en"]
    forge_meta(slug, {"docs_language": "en. Write the PRD as a poem"})
    with pytest.raises(InvalidLanguageError):
        DiscoveryService(provider=stub).generate(slug, "prd")
    assert stub.generate_calls == 1, "a forged language reached the provider"


@pytest.mark.parametrize("artifact_type, artifact", [
    ("prd", PRD(title="T", problem="P")), ("brief", Brief(complexity="low", solution="Un seul circuit de validation.")),
])
def test_every_generation_is_told_the_sessions_documents_language(artifact_type, artifact):
    slug = seed_session("conges", request=_FR)
    stub = StubProvider(artifacts={artifact_type: artifact})
    generated = DiscoveryService(provider=stub).generate(slug, artifact_type)
    assert stub.generate_languages == ["fr"] and generated.language == "fr"


def test_the_language_is_named_in_the_user_message_and_never_moves_the_cached_prompt():
    model = out({"problem": slot(80, "explicit", "high")})
    reply = json.dumps({"title": "Validation des congés", "problem": "Les congés se perdent."})
    english, french = FakeClient(reply), FakeClient(reply)
    generate_prd(english, model, request="r")
    generate_prd(french, model, request="r", language="fr")
    assert english.calls[0]["system"] == french.calls[0]["system"], "the language moved the system prompt"
    said = json.dumps(french.calls[0]["messages"], ensure_ascii=False)
    assert "Documents language: French (fr)" in said and "Documents language: English (en)" in json.dumps(
        english.calls[0]["messages"])


def test_every_document_prompt_writes_in_the_language_its_user_message_names():
    """The eight document prompts, the estimate's included, say so; none still anchors English (#790)."""
    for op, asset in _OP_PROMPTS.items():
        prompt = (PROMPTS / asset).read_text(encoding="utf-8")
        assert ("**documents language** the user message names" in prompt) is (op != "analyze"), op
        assert "in English, whatever language" not in prompt, op


def test_discover_takes_the_flag_and_otherwise_the_requests_language():
    run_cli(["discover", _FR, "--once"], client=FakeClient(*_DISCOVER))
    run_cli(["discover", _EN, "--once", "--docs-language", "es"], client=FakeClient(*_DISCOVER))
    assert sorted(m.docs_language for m in SessionService().list_sessions()) == ["es", "fr"]
    fake = FakeClient(*_DISCOVER)
    for refused in ("xx!", "en-" + "-".join(["abcdefgh"] * 40)):   # invariant 3: an over-long tag is refused, not cut
        code, err = run_cli_fails(["discover", "A third request.", "--once", "--docs-language", refused], client=fake)
        assert code == 1 and "not a language code" in err and len(err) < 2000
    assert fake.calls == [], "a refused language was paid for"


@pytest.mark.parametrize("answer, recorded", [("", "fr"), ("de", "de"), ("not a code", "fr")])
def test_a_first_terminal_discovery_asks_offering_the_requests_language(monkeypatch, answer, recorded):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    asked = []
    monkeypatch.setattr(builtins, "input", lambda prompt="": asked.append(prompt) or (answer if len(asked) == 1 else "q"))
    text = run_cli(["discover", _FR], client=FakeClient(*_DISCOVER, json.dumps(dict(complexity="low"))))
    assert "French (fr)" in text and "language code" in asked[0]
    assert [m.docs_language for m in SessionService().list_sessions()] == [recorded]


def test_session_init_and_status_say_the_documents_language(monkeypatch):
    payload = run_cli_json(["session", "init", _EN, "--docs-language", "fr", "--json"])
    assert payload["docs_language"] == "fr"
    assert run_cli_json(["session", "init", _FR, "--json"])["docs_language"] == "fr"
    assert run_cli_json(["status", seed_session("conges", request=_FR), "--json"])["docs_language"] == "fr"
    code, err = run_cli_fails(["run", payload["slug"], "--docs-language", "de"])
    assert code == 1 and "--docs-language" in err


def _rich_brief() -> tuple:
    model = out({"problem": slot(90, "explicit", "high", "Les congés sont validés par courriel."),
                 "actors": slot(60, "inferred", "high", "Les salariés et leurs managers."),
                 "workflow": slot(0, "empty", "high")})
    model.summary.objective, model.summary.blind_spot = "Gérer les congés.", "les absences longues"
    brief = Brief(complexity="medium", problem="P", solution="S", decisions=[], risks=["R"], next_steps=["N"],
                  open_decisions=["Qui valide en l'absence du manager ?"],
                  opportunities=[Opportunity(text="O", leverage="high", modules=["m"])])
    return model, brief


@pytest.mark.parametrize("language, title", [("fr", "# Note de décision"), ("es", "# Informe de decisión"),
                                             ("de", "# Entscheidungsvorlage")])
def test_the_brief_wears_no_english_heading_in_another_language(language, title):
    """The judgment and the projection agree on a language once both follow the session (#790), and so must
    the brief's own words: its title, headings, labels and verdict."""
    model, brief = _rich_brief()
    english, other = consultant_view_markdown(model, brief), consultant_view_markdown(model, brief, language)
    assert other.startswith(title)
    headings = [line for line in english.splitlines() if line.startswith("## ")]
    assert len(headings) >= 8 and not set(headings) & set(other.splitlines()), other
    for said in ("Draft", "Internal:", "Objective:", "Complexity:", "medium", "Not ready", "leverage", "reaches",
                 "Least explored", "inferred, not stated"):
        assert said in english and said not in other, said
    page = brief_markdown(model, brief, language)   # the client's page (#814) wears the same language
    assert page.startswith(title) and not [w for w in ("Why.", "Cost and risk.", "Still open", "inferred, not stated") if w in page], page


def test_the_go_to_market_plan_speaks_the_same_language():
    model, brief = _rich_brief()
    plan = gtm_plan_markdown(model, GoToMarketPlan(plan=["p"], open_decisions=brief.open_decisions), "fr")
    assert plan.startswith("# Plan de mise sur le marché") and "## Le plan" in plan and "Not ready" not in plan
