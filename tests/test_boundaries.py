"""Merged into `tests/test_source_form.py`'s Boundaries section (#551).

This stub stays on disk, empty of tests, for one reason only: `tests/test_boundaries.py` is cited by
its bare module name from `src/`, `CLAUDE.md` and `docs/` in ~40 places, and this repository's own
narrative-reference guard (`test_every_named_test_reference_resolves`, now in `test_source_form.py`)
requires that name to resolve to a real module stem. Moving the guard's own content over touching
those citations would mean a diff under `src/`, which #551 holds at zero. See `test_source_form.py`
for the actual tests, and `tests/test_encoding.py` -- deleted outright, since "test_encoding" is one
character short of this guard's own ten-character reference floor and needed no such stub.
"""
