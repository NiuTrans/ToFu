"""Small stateless helpers for conversation configuration and branch labels.

Transcript and branch mutations belong exclusively to Conversation Sync v3.
This blueprint contains only pure request/response adapters that do not touch
conversation storage.
"""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_bad_request, api_ok
from lib.branch_meta import classify_branch_title
from lib.conv_config import resolve_conv_config, resolve_conv_settings
from lib.openapi import api_meta
from lib.request_parser import (
    BadRequest,
    async_parse_body,
    optional_bool,
    optional_dict,
    require_str,
)

from .auth import require_scope


api_v1_conversations_bp = Blueprint("api_v1_conversations", __name__)


@api_v1_conversations_bp.route(
    "/api/v1/conversations/config/resolve", methods=["POST"]
)
@require_scope("conversations")
@api_meta(
    summary="Resolve runtime configuration for a conversation command",
    tags=["conversations"],
    scope="conversations",
)
async def resolve_config_route():
    body = await async_parse_body()
    conv_settings = optional_dict(body, "conv_settings", default={}) or {}
    overrides = optional_dict(body, "overrides", default={}) or {}
    resolved = resolve_conv_config(
        conv_settings=conv_settings,
        overrides=overrides,
        server_defaults=optional_dict(body, "server_defaults", default={}) or {},
        is_active=bool(body.get("is_active", True)),
    )
    if optional_bool(body, "include_settings", default=False):
        settings_conv = optional_dict(
            body,
            "settings_conv_settings",
            default=conv_settings,
        )
        resolved["settings"] = resolve_conv_settings(
            conv_settings=settings_conv,
            overrides=overrides,
        )
    return api_ok(resolved)


@api_v1_conversations_bp.route(
    "/api/v1/conversations/settings/resolve", methods=["POST"]
)
@require_scope("conversations")
@api_meta(
    summary="Resolve settings for persistence",
    tags=["conversations"],
    scope="conversations",
)
async def resolve_settings_route():
    body = await async_parse_body()
    return api_ok(resolve_conv_settings(
        conv_settings=optional_dict(body, "conv_settings", default={}) or {},
        overrides=optional_dict(body, "overrides", default={}) or {},
    ))


@api_v1_conversations_bp.route(
    "/api/v1/conversations/branches/classify", methods=["POST"]
)
@require_scope("conversations")
@api_meta(
    summary="Classify a branch title",
    tags=["conversations"],
    scope="conversations",
)
async def classify_branch():
    body = await async_parse_body()
    try:
        title = require_str(body, "title", max_len=200, allow_empty=True)
    except BadRequest as exc:
        return api_bad_request(str(exc), field=exc.field or "title")
    return api_ok(classify_branch_title(title))


__all__ = ["api_v1_conversations_bp"]
