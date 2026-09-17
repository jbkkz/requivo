---
title: "The persistence diagnostics tier is frozen"
description: "No new report-only diagnostic without a reproduced field instance and a user action (#287, #210)."
match: ^src/requivo/(core/persistence/scan\.py|deterministic/doctor\.py)$
---

The report-only diagnostics (`scan_session_root`, `scan_lock_root`, the non-session and lock-residue
rows) grew faster than the states they report occur, and each row mints public `--json` surface
that `docs/compatibility.md` then makes expensive to remove.

**No new report-only diagnostic lands here without a reproduced field instance of the state it
reports, and it must name what a user should do about it.** A residue with no action is not
reported. The one addition that would clear the bar is the stale dot-prefixed staging/backup class
(`create_session` staging dirs, `_swap_in`'s `.replaced`, `session restore`'s `.restore.tmp`).
