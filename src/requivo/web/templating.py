"""Shared Jinja2 environment and template/static locations, resolved from inside the package.
Autoescaping is on; `| safe` only for content the app produced. `csrf_token` is a global, so a route
cannot forget it and render buttons that silently 403.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from requivo.web.config import MAX_ANSWERS_CHARS, MAX_REQUEST_CHARS
from requivo.web.security import CSRF_FIELD, csrf_token
from requivo.web.viewmodels.labels import EXAMPLE_BADGE, UNREADABLE_BADGE, human_time

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["csrf_field"] = CSRF_FIELD
templates.env.globals["csrf_token"] = csrf_token()
# Registered here, decided in `labels.py` (#237).
templates.env.filters["human_time"] = human_time
# The input ceilings as globals, so the number shown and the number refused on cannot drift (#239):
# `test_the_limit_the_page_shows_is_the_limit_the_server_refuses_on`.
templates.env.globals["max_request_chars"] = MAX_REQUEST_CHARS
templates.env.globals["max_answers_chars"] = MAX_ANSWERS_CHARS
# The example badge (#226), decided in `labels.py`, read in more than one template.
templates.env.globals["example_badge"] = EXAMPLE_BADGE
# The third state's word (#240): two surfaces, one constant.
templates.env.globals["unreadable_badge"] = UNREADABLE_BADGE
