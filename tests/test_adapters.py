"""Tracker adapters: pure transforms over the neutral epic export (#72)."""
import json

from requivo.core.adapters import EPIC_EXPORT_VERSION, epic_export, epic_export_json, to_github, to_gitlab
from requivo.core.contracts import Epic


def test_epic_export_is_neutral_and_maps_issues():
    epic = Epic(
        title="Leave approval",
        milestone="Pilot",
        goal="Employees request leave, managers approve.",
        business_value="Removes email/Excel churn.",
        in_scope=["Submission"],
        issues=[
            {"id": "#1", "title": "Model the leave object", "labels": ["backend"]},
            {"id": "#2", "title": "Approval circuit", "labels": ["feature"], "depends_on": ["#1"]},
        ],
    )
    payload = epic_export(epic, "leave-approval", 3)
    # This line reads as the version guard and is not one (#267).
    assert payload["format"] == "requivo-epic" and payload["version"] == EPIC_EXPORT_VERSION
    assert payload["epic"]["labels"] == ["epic"] and payload["epic"]["milestone"] == "Pilot"
    # goal + business value + scope fold into one importable description body.
    assert "Business value" in payload["epic"]["description"]
    assert "In scope" in payload["epic"]["description"]
    # Each issue carries its ref, the shared milestone, and dependencies as refs.
    assert payload["issues"][0]["ref"] == "#1" and payload["issues"][0]["milestone"] == "Pilot"
    assert payload["issues"][1]["depends_on"] == ["#1"]
    # The JSON writer emits valid, parseable JSON.
    assert json.loads(epic_export_json(epic, "leave-approval", 3)) == payload


def test_epic_export_carries_the_session_slug_and_the_revision_it_was_rendered_from():
    """#274: epic.json is the machine-consumed input an n8n flow acts on."""
    epic = Epic(title="Leave approval", milestone="Pilot", issues=[{"id": "#1", "title": "T"}])
    payload = epic_export(epic, "leave-approval", 7)
    assert payload["slug"] == "leave-approval"
    assert payload["source_revision"] == 7
    # A different revision must produce a different stamp.
    other = epic_export(epic, "leave-approval", 8)
    assert other["source_revision"] == 8


def test_to_github_plan_degrades_honestly_and_is_idempotent():
    epic = Epic(
        title="Leave approval",
        milestone="Pilot",
        goal="Employees request leave.",
        issues=[
            {"id": "#1", "title": "Model the leave object", "description": "Fields.", "labels": ["backend"]},
            {"id": "#2", "title": "Approval circuit", "labels": ["feature"], "depends_on": ["#1"]},
        ],
    )
    plan = to_github(epic_export(epic, "leave-approval", 4), "leave-approval")
    assert plan["target"] == "github"
    assert plan["source_revision"] == 4
    # Every issue carries the idempotency label so a re-run can find-then-skip.
    label = "requivo-epic:leave-approval"
    assert plan["idempotency_label"] == label
    assert all(label in issue["labels"] for issue in plan["issues"])
    assert label in plan["tracking_issue"]["labels"]
    # The epic degrades to a tracking issue with a task list (GitHub has no native epic).
    assert "- [ ] Model the leave object" in plan["tracking_issue"]["body"]
    # depends_on has no native GitHub concept — stated in the body, resolved to the issue's title.
    assert "**Depends on:** Model the leave object" in plan["issues"][1]["body"]
    assert "_Part of epic: Leave approval_" in plan["issues"][0]["body"]


def test_to_gitlab_wires_depends_on_as_issue_links():
    epic = Epic(
        title="Leave approval",
        milestone="Pilot",
        issues=[
            {"id": "#1", "title": "Model the leave object", "labels": ["backend"]},
            {"id": "#2", "title": "Approval circuit", "labels": ["feature"], "depends_on": ["#1"]},
            {"id": "#3", "title": "UI", "labels": ["frontend"], "depends_on": ["#1", "#2"]},
        ],
    )
    plan = to_gitlab(epic_export(epic, "leave-approval", 9), "leave-approval")
    assert plan["target"] == "gitlab"
    assert plan["source_revision"] == 9
    label = "requivo-epic:leave-approval"
    assert all(label in issue["labels"] for issue in plan["issues"])
    # GitLab maps depends_on to structured issue links (the dependency blocks the dependent), not text.
    assert {"source_ref": "#1", "target_ref": "#2", "type": "blocks"} in plan["links"]
    assert {"source_ref": "#2", "target_ref": "#3", "type": "blocks"} in plan["links"]
    assert len(plan["links"]) == 3
    # No dependency text in the body — the relationship is structured.
    assert "Depends on" not in plan["issues"][1]["description"]
