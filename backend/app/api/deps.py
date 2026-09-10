"""Shared FastAPI dependencies for API route handlers."""

from app.db.session import get_db

__all__ = ["get_db"]
