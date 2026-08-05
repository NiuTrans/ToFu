"""lib/browser/handlers/_preview.py — Server-side page preview handler.

Unlike every sibling handler, ``browser_preview_page`` does NOT talk to the
browser extension: it renders in the shared headless Playwright pool on the
server (lib/browser/preview.py). It only shares the browser-family dispatch
so the tool-result/frontend plumbing (imageDataUris, badges) stays single.
"""

from lib.browser.preview import render_page_preview
from lib.log import get_logger

logger = get_logger(__name__)


def _handle_preview_page(fn_args):
    # Injected by the tasks_pkg browser handler — see
    # lib/tasks_pkg/handlers/browser.py. Handlers on this table receive
    # fn_args only, so the workspace root arrives as an internal arg.
    project_path = fn_args.get('_projectPath')
    wait_ms = fn_args.get('waitMs', fn_args.get('wait_ms', 1500))
    try:
        wait_ms = int(wait_ms)
    except (TypeError, ValueError) as e:
        logger.debug('[Preview] bad waitMs value (%s) — using default 1500', e)
        wait_ms = 1500
    return render_page_preview(
        project_path=project_path,
        path=fn_args.get('path') or None,
        url=fn_args.get('url') or None,
        width=fn_args.get('width') or 1280,
        height=fn_args.get('height') or 800,
        full_page=bool(fn_args.get('fullPage', False)),
        wait_ms=wait_ms,
    )
