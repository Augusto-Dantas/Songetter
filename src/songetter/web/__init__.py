"""FastAPI web interface and REST API package for Songetter."""

from __future__ import annotations

from songetter.web.app import app, create_app

__all__ = ["app", "create_app"]
