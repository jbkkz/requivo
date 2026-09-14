"""Split into `tests/test_plugin_cli_drift_resolution.py`, `tests/test_plugin_cli_drift_forgery_guards.py`
and `tests/test_plugin_cli_drift_platform.py` (#555): the original file grew past the module ceiling.

Kept on disk, empty of tests, because `tests/test_plugin_cli_drift.py` is cited by its bare module
name from `scripts/plugin_cli_drift.py`, `docs/decisions/0017-plugin-skills-mirror-a-pinned-cli-commit.md`
and several other test files. See the three files above for the actual tests.
"""
