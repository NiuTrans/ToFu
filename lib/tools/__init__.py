"""lib/tools/ — Tool execution engine package.

Sub-modules:
  search       — web_search, site_search tool definitions
  browser      — browser navigation, screenshot, click, type tools
  code_exec    — code execution tool definitions
  conversation — conversation reference tools
  image_gen    — image generation constants (tool names for display dispatch)
  meta         — build_project_tool_meta (dynamic tool list assembly)
  project      — project-mode file tools (read, write, grep, etc.)
  emit         — emit_to_user terminal tool (reference existing tool results)
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
    deferral,
    emit,
    human_guidance,
    image_gen,
    meta,
    project,
    search,
)
from .browser import *  # noqa: F401,F403
from .code_exec import *  # noqa: F401,F403
from .conversation import *  # noqa: F401,F403
from .deferral import *  # noqa: F401,F403
from .emit import *  # noqa: F401,F403
from .human_guidance import *  # noqa: F401,F403
from .image_gen import *  # noqa: F401,F403
from .meta import *  # noqa: F401,F403
from .project import *  # noqa: F401,F403
from .search import *  # noqa: F401,F403

build_facade(__all__, search, browser, code_exec, conversation, image_gen, meta, project, human_guidance, emit, deferral)
