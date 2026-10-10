"""lib/openapi/_docs.py — Swagger UI / ReDoc embed pages.

Single-file CDN bundles (no build step — meets CLAUDE.md §3.2 "no
frameworks, no build step"). Each returns a complete HTML document that
points at ``spec_url`` for the live OpenAPI JSON.
"""

from __future__ import annotations

import json

from lib.log import get_logger

logger = get_logger(__name__)


def swagger_html(spec_url: str = '/api/openapi.json') -> str:
    """Return a single-file Swagger UI HTML page (CDN, no build step)."""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Tofu API · Swagger UI</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <style>
    body {{ margin: 0; background: #1e1e1e; }}
    .topbar {{ display: none; }}
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.ui = SwaggerUIBundle({{
      url: {json.dumps(spec_url)},
      dom_id: '#swagger-ui',
      deepLinking: true,
      persistAuthorization: true,
      displayRequestDuration: true,
      filter: true,
      tryItOutEnabled: true,
      defaultModelsExpandDepth: 1,
    }});
  </script>
</body>
</html>"""


def redoc_html(spec_url: str = '/api/openapi.json') -> str:
    """Return a single-file ReDoc HTML page."""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Tofu API · ReDoc</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>body {{margin: 0;}}</style>
</head>
<body>
  <redoc spec-url={json.dumps(spec_url)}></redoc>
  <script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script>
</body>
</html>"""
