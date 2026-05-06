"""Shared Jinja2 environment for the whole app.

Every router that renders HTML imports ``templates`` from here. Building a
second ``Jinja2Templates`` elsewhere would silently drop the context
processors registered below (today: CSRF, flash messages) — every template
rendered through that second instance would render with empty
``csrf_token`` and no ``flash``, and the breakage would only surface when a
form-post 403'd or a confirmation message went missing.

Why a module global instead of an injection helper
--------------------------------------------------
Earlier slices (S1, S2) used an ``init_templates(t)`` shim per router so the
shared instance from the app factory could be passed in. That made the
``templates`` reference late-bound and forced a runtime check every render.
With two routers it was acceptable; with the third (taxonomy in S3) the
duplication crossed the threshold for extraction. A module-level global is
fine here because the configuration is pure, deterministic, and side-effect
free — no DB handle, no env-dependent secret, just two pure functions wired
into Jinja's context-processor list.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.csrf import csrf_context_processor


def flash_context_processor(request: Request) -> dict[str, str | None]:
    """Pop a one-shot flash message into the template context.

    Routes set ``request.session["flash"]`` after a successful POST. The base
    layout renders it once and the entry is consumed here so the next page
    load is clean.
    """
    flash = request.session.pop("flash", None) if "session" in request.scope else None
    return {"flash": flash}


templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates"),
    context_processors=[csrf_context_processor, flash_context_processor],
)
