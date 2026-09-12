from __future__ import annotations

import json

from requivo.core.contracts import Epic

EPIC_EXPORT_FORMAT = "requivo-epic"


EPIC_EXPORT_VERSION = 2


def epic_export(epic: Epic, slug: str, source_revision: int) -> dict:
    """A tool-neutral, importable view of an epic — maps cleanly onto GitHub or GitLab issues.

    A stable, versioned envelope an importer (or an n8n flow) can validate and feed to either
    tracker's API. The epic becomes a tracking issue / GitLab epic; each issue keeps its labels,
    the shared milestone, and `depends_on` as issue refs so relationships can be wired after create.

    `slug` and `source_revision` (#274) stamp *provenance*, not freshness: this envelope is written
    outside `ArtifactService`, deliberately, so the staleness graph never tracks it the way `epic.md`
    is tracked, and without a revision an automation consuming it had no signal that the session had
    moved on. The basis it names is compared against `requivo status --json`'s `artifacts.epic.stale`,
    never against a revision number directly (invariant 1). Pinned by
    `test_epic_export_carries_the_session_slug_and_the_revision_it_was_rendered_from`.
    """
    description = "\n\n".join(
        part
        for part in [
            epic.goal,
            f"**Business value:** {epic.business_value}" if epic.business_value else "",
            ("**In scope:**\n" + "\n".join(f"- {i}" for i in epic.in_scope)) if epic.in_scope else "",
            ("**Out of scope:**\n" + "\n".join(f"- {i}" for i in epic.out_of_scope)) if epic.out_of_scope else "",
        ]
        if part
    )
    return {
        "format": EPIC_EXPORT_FORMAT,
        "version": EPIC_EXPORT_VERSION,
        "slug": slug,
        "source_revision": source_revision,
        "epic": {
            "title": epic.title,
            "description": description,
            "labels": ["epic"],
            "milestone": epic.milestone,
        },
        "issues": [
            {
                "ref": issue.id,
                "title": issue.title,
                "description": issue.description,
                "labels": issue.labels,
                "milestone": epic.milestone,
                "depends_on": issue.depends_on,
            }
            for issue in epic.issues
        ],
        "open_questions": epic.open_questions,
    }


def epic_export_json(epic: Epic, slug: str, source_revision: int) -> str:
    return json.dumps(epic_export(epic, slug, source_revision), indent=2) + "\n"


def to_github(export: dict, slug: str) -> dict:
    """Adapter: neutral epic export → a GitHub issue-creation plan (pure, no network).

    An automation (e.g. an n8n flow) creates the child issues first, then the tracking issue.
    GitHub has no native epic or issue dependency, so we degrade honestly: the epic becomes a
    tracking issue with a task list, and `depends_on` is stated in each issue body. Every issue
    carries an idempotency label (`requivo-epic:<slug>`) so a re-run can find-then-skip existing issues
    instead of duplicating. `milestone` is a name — the automation resolves it to GitHub's numeric id.
    """
    label = f"requivo-epic:{slug}"
    title_by_ref = {i["ref"]: i["title"] for i in export["issues"]}
    epic_title = export["epic"]["title"]

    def child_body(issue: dict) -> str:
        parts = [issue["description"]] if issue["description"] else []
        deps = [title_by_ref.get(ref, ref) for ref in issue.get("depends_on", [])]
        if deps:
            parts.append("**Depends on:** " + ", ".join(deps))
        parts.append(f"_Part of epic: {epic_title}_")
        return "\n\n".join(parts)

    task_list = "\n".join(f"- [ ] {i['title']}" for i in export["issues"])
    tracking_body = (export["epic"]["description"] + "\n\n### Issues\n\n" + task_list).strip()

    return {
        "target": "github",
        "source_revision": export["source_revision"],
        "idempotency_label": label,
        "tracking_issue": {
            "title": f"Epic: {epic_title}",
            "body": tracking_body,
            "labels": export["epic"]["labels"] + [label],
            "milestone": export["epic"]["milestone"],
        },
        "issues": [
            {
                "ref": issue["ref"],
                "title": issue["title"],
                "body": child_body(issue),
                "labels": issue["labels"] + [label],
                "milestone": issue["milestone"],
            }
            for issue in export["issues"]
        ],
    }


def to_github_json(epic: Epic, slug: str, source_revision: int) -> str:
    return json.dumps(to_github(epic_export(epic, slug, source_revision), slug), indent=2) + "\n"


def to_gitlab(export: dict, slug: str) -> dict:
    """Adapter: neutral epic export → a GitLab issue-creation plan (pure, no network).

    GitLab maps more faithfully than GitHub: `depends_on` becomes structured issue `links`
    (`blocks`) an automation wires after create — not body text. Native Epics are Premium-only, so
    for portability the epic is a tracking issue with a task list on any tier. Each issue carries the
    `requivo-epic:<slug>` idempotency label; `milestone` is a name the automation resolves to its id.
    """
    label = f"requivo-epic:{slug}"
    epic_title = export["epic"]["title"]

    def child_description(issue: dict) -> str:
        parts = [issue["description"]] if issue["description"] else []
        parts.append(f"_Part of epic: {epic_title}_")
        return "\n\n".join(parts)

    task_list = "\n".join(f"- [ ] {i['title']}" for i in export["issues"])
    tracking_description = (export["epic"]["description"] + "\n\n### Issues\n\n" + task_list).strip()

    # depends_on: the dependency blocks the dependent issue. Wire as GitLab issue links after create.
    links = [
        {"source_ref": ref, "target_ref": issue["ref"], "type": "blocks"}
        for issue in export["issues"]
        for ref in issue.get("depends_on", [])
    ]

    return {
        "target": "gitlab",
        "source_revision": export["source_revision"],
        "idempotency_label": label,
        "tracking_issue": {
            "title": f"Epic: {epic_title}",
            "description": tracking_description,
            "labels": export["epic"]["labels"] + [label],
            "milestone": export["epic"]["milestone"],
        },
        "issues": [
            {
                "ref": issue["ref"],
                "title": issue["title"],
                "description": child_description(issue),
                "labels": issue["labels"] + [label],
                "milestone": issue["milestone"],
            }
            for issue in export["issues"]
        ],
        "links": links,
    }


def to_gitlab_json(epic: Epic, slug: str, source_revision: int) -> str:
    return json.dumps(to_gitlab(epic_export(epic, slug, source_revision), slug), indent=2) + "\n"
