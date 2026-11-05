"""Public template port for manifest-owned plugin browser dependencies.

Entry: register_plugin_frontend(app), called by application assembly. Plugins
use the Jinja helper without importing host implementation modules. Manifest
reads remain off the serving loop and use the existing Vite delivery owner.
"""
from __future__ import annotations

import asyncio
from typing import Any

from markupsafe import Markup

from lib.vite_assets import get_vite_asset_tags


async def _plugin_asset_tags() -> Markup:
    return Markup(await asyncio.to_thread(get_vite_asset_tags, 'plugin'))


def register_plugin_frontend(app: Any) -> None:
    """Install the host's public async Jinja helper on this app instance."""
    app.jinja_env.globals['tofu_plugin_assets'] = _plugin_asset_tags
