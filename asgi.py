"""ASGI application entry for ``hypercorn asgi:app``."""

from server import create_production_app


app = create_production_app()


__all__ = ['app']
