"""lib/skills/ — Skill accumulation system: storage, injection, tool definitions.

Façade package — all public API is re-exported here::

    from lib.skills import create_skill, update_skill, delete_skill, merge_skills
    from lib.skills import list_all_skills, get_skill, build_skills_context
    from lib.skills import ALL_SKILL_TOOLS, SKILL_TOOL_NAMES
    from lib.skills import SKILL_ACCUMULATION_INSTRUCTIONS
    from lib.skills import SKILL_ACCUMULATION_INSTRUCTIONS_COMPACT
    from lib.skills import filter_relevant_skills
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
