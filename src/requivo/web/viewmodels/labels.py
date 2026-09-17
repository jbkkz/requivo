"""The user-facing vocabulary, one table read by every template: a translation over the same values,
changing nothing that is stored, computed or emitted by `--json`.

    requirements model  →  current understanding      artifact        →  document
    explicit evidence   →  what we know               stale artifact  →  needs updating
    inferred evidence   →  what we are assuming       revision        →  history
    unknown             →  open question              context card    →  product context
    testable evidence   →  to test                    challenge       →  assumption to review
    readiness           →  are we ready?              provider        →  advanced setting
                                                        slot            →  (never shown by default)
"""

from __future__ import annotations

from datetime import datetime, timezone

# Artifact type → the name a reader sees, wider than what the Web generates. `brief` changed name and
# not identity: the file, the verb and the contract are unchanged.
ARTIFACT_LABELS: dict[str, str] = {
    "brief": "Decision brief",
    "prd": "PRD",
    "stories": "User stories",
    "estimate": "Estimate",
    "criteria": "Acceptance criteria",
    "epic": "Delivery epic",
    "release": "Release notes",
    "gtm_plan": "Go-to-market plan",  # #609 -- the go-to-market perimeter's one artifact
}

# Each perimeter's primary document is `Perimeter.primary_artifact` in `core/` (#609); this module owns the caption.

# ── the bundled example (#226): one word, registered as a Jinja global in `templating.py` ──
EXAMPLE_BADGE = "Example"

# ── a session nobody could read (#240) ────────────────────────────────────────
# `UNREADABLE_HINT` replaces jargon under a row, except where the store's message is already
# reader-facing (`test_a_failure_already_written_for_a_reader_survives_to_the_row`). No apostrophe:
# autoescaping would turn it into `&#39;`.
UNREADABLE_BADGE = "Could not be read"
UNREADABLE_HINT = ("Requivo could not read the files for this session. "
                   "Open it for the full detail.")
# Appended to a message shown as-is.
_OPEN_IT = " Open it for the full detail."

# How long a failure may be and still lead a row, and what disqualifies it; both fail towards the
# generic sentence. The separators catch an absolute path on either platform, `Errno` the OSError family.
_MAX_ROW_FAILURE_CHARS = 160
_MACHINE_MARKERS = ("/", "\\", "Errno")


def unreadable_hint(error: str | None) -> str:
    """The one line a degraded home row shows: a failure already written for a reader, with a pointer
    appended, or `UNREADABLE_HINT`. A test on the text, since the view model is handed `str(e)`.
    `test_a_degraded_row_shows_one_human_line_and_no_engine_internals`."""
    if not error:
        return UNREADABLE_HINT
    text = error.strip()
    if (len(text) <= _MAX_ROW_FAILURE_CHARS and "\n" not in text
            and not any(m in text for m in _MACHINE_MARKERS)):
        return text + _OPEN_IT
    return UNREADABLE_HINT


def artifact_label(artifact_type: str) -> str:
    return ARTIFACT_LABELS.get(artifact_type, artifact_type)


def artifact_labels(types: list[str]) -> list[str]:
    return [artifact_label(t) for t in types]


# ── when something last moved ─────────────────────────────────────────────────
# The store's `2026-08-25T12:36:48Z` is right for `--json` and wrong for a screen. Spelled out rather
# than `strftime`: `%b` is locale-dependent and the no-pad day differs between POSIX and Windows.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Past a week, "N days ago" stops being easier to read than the date.
_RELATIVE_DAYS = 7


def _instant(value: str) -> datetime | None:
    """One persisted timestamp as an aware datetime, or None; `fromisoformat` rejects `Z` before 3.11,
    and a stamp with no zone is read as UTC, which is what the store writes."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def human_time(value: str | None, *, now: str | None = None) -> str:
    """When a session last moved, in the screen's vocabulary: a relative time or the date; nothing
    for an empty value (a degraded row states no time, invariant 15); a stamp it cannot read handed
    back unchanged. `now` is injectable; a future stamp clamps to "just now".
    `test_human_time_hands_back_a_stamp_it_could_not_read_rather_than_hiding_it`."""
    if not value:
        return ""
    stamp = _instant(value)
    if stamp is None:
        return value
    reference = _instant(now) if now else datetime.now(timezone.utc)
    if reference is None:
        return value

    seconds = max(0.0, (reference - stamp).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < _RELATIVE_DAYS:
        return f"{days} days ago"
    return f"{stamp.day} {_MONTHS[stamp.month - 1]} {stamp.year}"
