"""FastAPI web interface and REST API package for Songetter."""

from __future__ import annotations

from songetter.web.app import create_app

__all__ = ["create_app"]
