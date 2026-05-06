"""Unit tests for ``app.template_env``.

The module exists so every router renders through one Jinja env with the
CSRF + flash context processors attached. If a future edit accidentally drops
either processor, every form would silently render with an empty
``csrf_token`` (POSTs would 403) or no ``flash`` (success messages would
disappear). These tests pin the contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.templating import Jinja2Templates

from app.csrf import csrf_context_processor
from app.template_env import flash_context_processor, templates


class TestSharedTemplatesInstance:
    def test_templates_is_a_jinja2templates(self) -> None:
        assert isinstance(templates, Jinja2Templates)

    def test_templates_directory_points_at_app_templates(self) -> None:
        # The Jinja loader's search path should include app/templates so
        # ``base.html`` etc. resolve.
        loader = templates.env.loader
        assert loader is not None
        # FileSystemLoader exposes ``searchpath``; we accept any loader that
        # has the templates dir on its search list.
        searchpaths = getattr(loader, "searchpath", [])
        assert any(p.endswith("templates") for p in searchpaths), searchpaths

    def test_csrf_and_flash_processors_are_registered(self) -> None:
        # Jinja2Templates stores configured context processors on
        # ``context_processors``; we don't want to depend on the private name
        # if it changes, so accept either attribute the framework exposes.
        processors = getattr(templates, "context_processors", None)
        if processors is None:
            processors = templates.env.globals.get("__context_processors__", [])
        assert csrf_context_processor in processors
        assert flash_context_processor in processors


class TestFlashContextProcessor:
    def test_pops_flash_from_session(self) -> None:
        # Build a fake request with a session dict containing a flash.
        request = MagicMock()
        request.scope = {"session": {}}
        request.session = {"flash": "hello"}

        result = flash_context_processor(request)

        assert result == {"flash": "hello"}
        # One-shot: the entry is gone after rendering.
        assert "flash" not in request.session

    def test_returns_none_when_no_flash(self) -> None:
        request = MagicMock()
        request.scope = {"session": {}}
        request.session = {}

        result = flash_context_processor(request)

        assert result == {"flash": None}

    def test_returns_none_when_no_session_in_scope(self) -> None:
        # Anonymous / pre-session paths must not crash.
        request = MagicMock()
        request.scope = {}

        result = flash_context_processor(request)

        assert result == {"flash": None}
