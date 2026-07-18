"""lib/openapi/_paths.py — Flask-rule ↔ OpenAPI-path helpers.

Pure string/introspection helpers used by :mod:`lib.openapi._spec`:

  * ``_FLASK_VAR_RE`` / ``_flask_to_openapi_path`` / ``_path_parameters``
    — rewrite ``<int:n>`` style rules into ``{n}`` and pull out the
    path parameters.
  * ``_SKIP_PATTERNS`` / ``_skip_rule`` — internal/static/legacy routes
    that never make it into the spec.
  * ``_auto_tags`` / ``_default_description`` — tagging heuristics and
    the boilerplate top-level description.
"""

from __future__ import annotations

import re

from lib.log import get_logger

logger = get_logger(__name__)


# ── Path conversion ─────────────────────────────────────────────────

_FLASK_VAR_RE = re.compile(r'<(?:[^:>]+:)?([^>]+)>')


def _flask_to_openapi_path(rule: str) -> str:
    """Convert ``/api/v1/tasks/<task_id>`` → ``/api/v1/tasks/{task_id}``."""
    return _FLASK_VAR_RE.sub(r'{\1}', rule)


def _path_parameters(rule: str) -> list[dict]:
    """Extract path parameters from a Flask rule into OpenAPI shape."""
    out = []
    for m in _FLASK_VAR_RE.finditer(rule):
        name = m.group(1)
        out.append({
            'name': name, 'in': 'path', 'required': True,
            'schema': {'type': 'string'},
        })
    return out


# ── Skip rules ─────────────────────────────────────────────────────

# Routes we explicitly skip — internal/static/legacy.
_SKIP_PATTERNS = (
    re.compile(r'^/static(/|$)'),
    re.compile(r'^/$'),
    re.compile(r'^/(trading\.html|index\.html|favicon\.ico)$'),
    re.compile(r'^/api/openapi\.'),
    re.compile(r'^/api/docs(/|$)'),
    re.compile(r'^/api/redoc(/|$)'),
)


def _skip_rule(path: str) -> bool:
    return any(p.match(path) for p in _SKIP_PATTERNS)


# ── Tagging + boilerplate ──────────────────────────────────────────

def _auto_tags(path: str) -> list[str]:
    if path.startswith('/api/v1/chat'):
        return ['chat']
    if path.startswith('/api/v1/tasks'):
        return ['tasks']
    if path.startswith('/api/v1/conversations'):
        return ['conversations']
    if path.startswith('/api/v1/capabilities'):
        return ['capabilities']
    if path.startswith('/api/v1/agents'):
        return ['agents']
    if path.startswith('/api/v1/keys'):
        return ['keys']
    if path.startswith('/api/v1/webhooks'):
        return ['webhooks']
    if path == '/v1/chat/completions' or path.startswith('/v1/chat'):
        return ['compat:openai']
    if path.startswith('/v1/messages'):
        return ['compat:anthropic']
    if path.startswith('/v1/'):
        return ['compat:openai']
    if path.startswith('/api/v1'):
        return ['v1']
    return ['legacy']


def _default_description() -> str:
    return (
        'Tofu is a self-hosted AI assistant. This OpenAPI spec covers '
        'three surfaces:\n\n'
        '* `/api/v1/*` — Tofu-native API. Stable, versioned, fully '
        'expressive (tool streams, plan/critic verdicts, swarm, paper, '
        'translate, scheduler, memory, MCP, trading).\n'
        '* `/v1/chat/completions`, `/v1/models`, `/v1/embeddings` — '
        'OpenAI-compatible adapter. Use any OpenAI SDK by pointing '
        '`base_url` at this server.\n'
        '* `/v1/messages` — Anthropic Messages API adapter. Use the '
        'Anthropic SDK by pointing `base_url` at this server.\n\n'
        'Auth: `Authorization: Bearer tofu_live_…` for headless callers, '
        'or `X-Tunnel-Token` / cookie for the browser UI. See '
        '[`docs/HEADLESS_API.md`](/docs/HEADLESS_API.md) for the full guide.'
    )
