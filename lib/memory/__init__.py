"""lib/memory/ — Memory accumulation system: storage, injection, tool definitions.

Façade package — all public API is re-exported here::

    from lib.memory import create_memory, update_memory, delete_memory, merge_memories
    from lib.memory import list_all_memories, get_memory, build_memory_context
    from lib.memory import ALL_MEMORY_TOOLS, MEMORY_TOOL_NAMES, SEARCH_MEMORIES_TOOL
    from lib.memory import MEMORY_ACCUMULATION_INSTRUCTIONS
    from lib.memory import MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT
    from lib.memory import filter_relevant_memories, search_memories
"""

from lib._pkg_utils import build_facade

__all__: list[str] = []

# ── Storage (CRUD, file I/O) ──
from . import storage
from .storage import *  # noqa: F401,F403

build_facade(__all__, storage)

# ── Injection (system prompt) ──
from . import injection
from .injection import *  # noqa: F401,F403

build_facade(__all__, injection)

# ── Relevance scoring ──
from . import relevance
from .relevance import *  # noqa: F401,F403

build_facade(__all__, relevance)

# ── Tool definitions ──
from . import tools
from .tools import *  # noqa: F401,F403

build_facade(__all__, tools)

# ── Per-turn prefetch (metadata BM25 → local confidence gate) ──
from . import prefetch
from .prefetch import *  # noqa: F401,F403

build_facade(__all__, prefetch)
