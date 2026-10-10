"""Browser AST inventory and import-safe production route assembly.

Entry points: browser_api_inventory, browser_route_app. Dependencies: Node's
TypeScript parser and routes.register_all; neither opens storage nor starts
workers. Dynamic caller/server-owned URL seams remain visible in the inventory.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import subprocess
import re

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def browser_api_inventory() -> dict:
    result = subprocess.run(
        ['node', 'scripts/frontend_api_inventory.mjs'], cwd=ROOT,
        capture_output=True, text=True, timeout=60, check=True,
    )
    inventory = json.loads(result.stdout)
    assert not inventory['errors'], inventory['errors']
    assert inventory['endpoints'], 'Browser inventory is empty'
    return inventory


@lru_cache(maxsize=1)
def browser_route_app():
    from scripts.gen_api_endpoint_index import route_app

    return route_app()


def route_failure(app, path: str, method: str | None) -> str | None:
    """Match through Werkzeug, including generic and typed route converters."""
    from werkzeug.exceptions import MethodNotAllowed, NotFound
    from werkzeug.routing import RequestRedirect, WebsocketMismatch

    concrete = re.sub(r'<[^>]+>', '1', path)
    adapter = app.url_map.bind('localhost')
    try:
        adapter.match(concrete, method=method or 'GET')
    except RequestRedirect:
        return None
    except MethodNotAllowed as error:
        if method is None:
            return None  # A URL builder may target a non-GET form/action.
        return f'{method} {path} allows {sorted(error.valid_methods or [])}'
    except (NotFound, WebsocketMismatch):
        # URL builders also address WebSocket endpoints owned by the registry.
        if method is None:
            try:
                adapter.match(concrete, websocket=True)
                return None
            except (NotFound, MethodNotAllowed, WebsocketMismatch):
                pass
        return f'{method or "URL"} {path} has no route'
    return None
