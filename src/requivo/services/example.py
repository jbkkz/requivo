"""The one thing every reader of the bundled example shares (#556): a byte-for-byte, UTF-8-declared
read of a file under `paths.DEMO` (invariant 16). `web/example.py`'s `seed_example` turns those
bytes into a real session; `cli.py`'s `demo` verb renders a different subset straight to the
terminal, with no session involved -- genuinely different purposes reading genuinely different
files, so only the read itself was duplicated. `web/example.py` keeps every policy decision about
the example (`is_example`, `seed_example`, `_unquote`); this module holds only the read beneath it.
"""

from __future__ import annotations

from requivo.paths import DEMO


def read_demo_asset(name: str) -> str:
    """One bundled demo/example asset under `paths.DEMO`, UTF-8 (invariant 16)."""
    return (DEMO / name).read_text(encoding="utf-8")
