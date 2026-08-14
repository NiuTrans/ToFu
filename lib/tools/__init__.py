"""lib/tools/ — Tool execution engine package.

Sub-modules:
  search       — web_search, site_search tool definitions
  browser      — browser navigation, screenshot, click, type tools
  code_exec    — code execution tool definitions
  conversation — conversation reference tools
  image_gen    — image generation constants (tool names for display dispatch)
  image_edit   — inspect_image constants (zoom/rotate/crop image viewer)
  meta         — build_project_tool_meta (dynamic tool list assembly)
  project      — project-mode file tools (read, write, grep, etc.)
"""

from lib._pkg_utils import build_facade
from lib.log import get_logger

_logger = get_logger(__name__)

__all__: list[str] = []

# ── Core modules (all required) ─────────────────────────────────────
from . import (  # noqa: E402
    browser,
    code_exec,
    conversation,
    human_guidance,
    image_edit,
    image_gen,
    meta,
    motion_video,
    project,
    search,
)
from .browser import *  # noqa: F401,F403
from .code_exec import *  # noqa: F401,F403
from .conversation import *  # noqa: F401,F403
from .human_guidance import *  # noqa: F401,F403
from .image_edit import *  # noqa: F401,F403
from .image_gen import *  # noqa: F401,F403
from .meta import *  # noqa: F401,F403
from .motion_video import *  # noqa: F401,F403
from .project import *  # noqa: F401,F403
from .search import *  # noqa: F401,F403

build_facade(__all__, search, browser, code_exec, conversation, image_gen, image_edit, meta, project, human_guidance, motion_video)

# ── Declarative tool-assembly registry (imported last: its built-in spec
#    builders lazily reference the schema constants above at request time) ──
from . import registry  # noqa: E402,F401  — side-effect import (registers specs)
from .registry import (  # noqa: E402,F401
    ToolContext,
    ToolSpec,
    all_specs,
    assemble_tool_list,
    available_plugins,
    discover_plugin_specs,
    register_tool_spec,
    resolve_enabled_plugins,
    sync_spec_handlers,
)

__all__ += [
    'ToolContext', 'ToolSpec', 'all_specs', 'assemble_tool_list',
    'available_plugins', 'discover_plugin_specs',
    'register_tool_spec', 'resolve_enabled_plugins', 'sync_spec_handlers',
]
