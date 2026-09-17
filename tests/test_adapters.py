"""Tracker adapters: pure transforms over the neutral epic export (#72)."""
import json

from requivo.core.adapters import EPIC_EXPORT_VERSION, epic_export, epic_export_json, to_github, to_gitlab
from requivo.core.contracts import Epic

_ISSUES = [{"id": "#1", "title": "Model the leave object", "description": "Fields.", "labels": ["backend"]},
           {"id": "#2", "title": "Approval circuit", "labels": ["feature"], "depends_on": ["#1"]}]
_LABEL = "requivo-epic:leave-approval"


def _epic(**extra) -> Epic:
    return Epic(title="Leave approval", milestone="Pilot", goal="Employees request leave, managers approve.", issues=_ISSUES, **extra)


def test_epic_export_is_neutral_and_maps_issues():
    payload = epic_export(_epic(business_value="Removes email/Excel churn.", in_scope=["Submission"]), "leave-approval", 3)
    assert payload["format"] == "requivo-epic" and payload["version"] == EPIC_EXPORT_VERSION  # reads as the version guard, is not (#267)
    assert payload["epic"]["labels"] == ["epic"] and payload["epic"]["milestone"] == "Pilot"
    assert "Business value" in payload["epic"]["description"] and "In scope" in payload["epic"]["description"]
    assert payload["issues"][0]["ref"] == "#1" and payload["issues"][0]["milestone"] == "Pilot"
    assert payload["issues"][1]["depends_on"] == ["#1"]
    assert json.loads(epic_export_json(_epic(business_value="Removes email/Excel churn.", in_scope=["Submission"]), "leave-approval", 3)) == payload


def test_epic_export_carries_the_session_slug_and_the_revision_it_was_rendered_from():
    """#274: epic.json is the machine-consumed input an n8n flow acts on."""
    assert epic_export(_epic(), "leave-approval", 7)["slug"] == "leave-approval"
    assert [epic_export(_epic(), "leave-approval", r)["source_revision"] for r in (7, 8)] == [7, 8]


def test_to_github_plan_degrades_honestly_and_is_idempotent():
    plan = to_github(epic_export(_epic(), "leave-approval", 4), "leave-approval")
    assert plan["target"] == "github" and plan["source_revision"] == 4
    # Every issue carries the idempotency label so a re-run can find-then-skip.
    assert plan["idempotency_label"] == _LABEL and all(_LABEL in i["labels"] for i in plan["issues"]) and _LABEL in plan["tracking_issue"]["labels"]
    # No native epic or dependency on GitHub: a tracking issue with a task list, depends_on stated in the body.
    assert "- [ ] Model the leave object" in plan["tracking_issue"]["body"]
    assert "**Depends on:** Model the leave object" in plan["issues"][1]["body"]
    assert "_Part of epic: Leave approval_" in plan["issues"][0]["body"]


def test_to_gitlab_wires_depends_on_as_issue_links():
    epic = Epic(title="Leave approval", milestone="Pilot",
                issues=_ISSUES + [{"id": "#3", "title": "UI", "labels": ["frontend"], "depends_on": ["#1", "#2"]}])
    plan = to_gitlab(epic_export(epic, "leave-approval", 9), "leave-approval")
    assert plan["target"] == "gitlab" and plan["source_revision"] == 9
    assert all(_LABEL in i["labels"] for i in plan["issues"])
    # depends_on maps to structured issue links (the dependency blocks the dependent), never text.
    assert {"source_ref": "#1", "target_ref": "#2", "type": "blocks"} in plan["links"]
    assert {"source_ref": "#2", "target_ref": "#3", "type": "blocks"} in plan["links"] and len(plan["links"]) == 3
    assert "Depends on" not in plan["issues"][1]["description"]
