"""lib/skills — User-installed skill packages (Anthropic AgentSkills format).

Skills are USER-curated instruction bundles — a separate noun from
memories (model-authored experience notes). This package owns the skills
channel end to end:

  • ``registry``   — enumerate / uninstall installed packages
  • ``injection``  — the always-visible ``<available_skills>`` index block
  • ``activate``   — ``activate_skill`` progressive-disclosure loader
  • ``tools``      — tool schema(s) for the agent loop
  • ``installer``  — zip → validated skill package on disk (user action)
  • ``catalog``    — curated App-Store-style catalog entries

Public API::

    from lib.skills import list_skills, get_skill, uninstall_skill
    from lib.skills import build_skills_index, activate_skill, list_skill_files
    from lib.skills import ACTIVATE_SKILL_TOOL, ALL_SKILL_TOOLS, SKILL_TOOL_NAMES
    from lib.skills import InstallerError, install_skill_package
    from lib.skills import get_catalog, get_catalog_entry
"""

from lib.skills.registry import (
    get_skill,
    list_skills,
    set_skill_scope,
    uninstall_skill,
)
from lib.skills.injection import (
    build_skills_index,
)
from lib.skills.activate import (
    activate_skill,
    list_skill_files,
)
from lib.skills.tools import (
    ACTIVATE_SKILL_TOOL,
    ALL_SKILL_TOOLS,
    SKILL_TOOL_NAMES,
)
from lib.skills.installer import (
    InstallerError,
    install_skill_package,
)
from lib.skills.catalog import (
    get_catalog,
    get_catalog_entry,
)

__all__ = [
    'list_skills', 'get_skill', 'uninstall_skill', 'set_skill_scope',
    'build_skills_index',
    'activate_skill', 'list_skill_files',
    'ACTIVATE_SKILL_TOOL', 'ALL_SKILL_TOOLS', 'SKILL_TOOL_NAMES',
    'InstallerError', 'install_skill_package',
    'get_catalog', 'get_catalog_entry',
]
