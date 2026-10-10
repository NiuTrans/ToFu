"""Register the paper-reading HTTP surface.

Responsibility: import each focused route module exactly once so it attaches
handlers to the shared paper blueprints. Business logic and task authorities
live under :mod:`lib.paper`; handlers live under :mod:`routes.paper_pkg`.

Entry point: :data:`paper_bp`, consumed by :mod:`routes`. The native v1
blueprint is owned by :mod:`routes.api_v1.paper` and joins ``ALL_V1_BLUEPRINTS``.
"""

from routes.paper_pkg._common import paper_bp

# Registration imports. Each module decorates either ``paper_bp`` or the
# shared native-v1 blueprint during import; none is an API re-export facade.
from routes.paper_pkg import _arxiv as _arxiv_routes  # noqa: F401
from routes.paper_pkg import _assets_review as _assets_review_routes  # noqa: F401
from routes.paper_pkg import _deepen_notes as _deepen_note_routes  # noqa: F401
from routes.paper_pkg import _library as _library_routes  # noqa: F401
from routes.paper_pkg import _pdf as _pdf_routes  # noqa: F401
from routes.paper_pkg import _podcast as _podcast_routes  # noqa: F401
from routes.paper_pkg import _qa_translate as _qa_translate_routes  # noqa: F401
from routes.paper_pkg import _recommend as _recommend_routes  # noqa: F401
from routes.paper_pkg import _report as _report_routes  # noqa: F401

__all__ = ['paper_bp']
