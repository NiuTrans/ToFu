"""lib/skills — User-installed skill packages (Anthropic AgentSkills format).

Skills are USER-curated instruction bundles — a separate noun from
memories (model-authored experience notes). This package owns the skills
channel end to end:

  • ``registry``   — enumerate / uninstall installed packages
  • ``injection``  — the always-visible ``<available_skills>`` index block
  • ``load``       — ``load_skill`` progressive-disclosure loader
  • ``tools``      — tool schema(s) for the agent loop
  • ``installer``  — zip → validated skill package on disk (user action)
  • ``catalog``    — curated App-Store-style catalog entries

Public API::

    from lib.skills import list_skills, get_skill, uninstall_skill
    from lib.skills import build_skills_index, load_skill, list_skill_files
    from lib.skills import LOAD_SKILL_TOOL, ALL_SKILL_TOOLS, SKILL_TOOL_NAMES
    from lib.skills import InstallerError, install_skill_package
    from lib.skills import get_catalog, get_catalog_entry
"""

from lib.skills.registry import (
    get_skill,
    list_skills,
    set_skill_enabled,
    set_skill_scope,
    uninstall_skill,
)
from lib.skills.injection import (
    build_skills_index,
)
from lib.skills.load import (
    load_skill,
    list_skill_files,
    read_skill_resource,
)
from lib.skills.tools import (
    SEARCH_SKILLS_TOOL,
    LOAD_SKILL_TOOL,
    READ_SKILL_RESOURCE_TOOL,
    REQUEST_SKILL_INSTALL_TOOL,
    ALL_SKILL_TOOLS,
    SKILL_INSTALL_TOOLS,
    SKILL_READ_TOOLS,
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
from lib.skills.catalog_install import (
    CatalogInstallError,
    install_catalog_skill,
)
from lib.skills.discovery import (
    render_skill_search,
    search_skill_catalog,
    search_skills,
)
from lib.skills.online_catalog import (
    OnlineCatalogError,
    install_clawhub_skill,
    search_online_skills,
)

__all__ = [
    'list_skills', 'get_skill', 'uninstall_skill', 'set_skill_enabled',
    'set_skill_scope',
    'build_skills_index',
    'load_skill', 'read_skill_resource', 'list_skill_files',
    'SEARCH_SKILLS_TOOL', 'LOAD_SKILL_TOOL', 'READ_SKILL_RESOURCE_TOOL',
    'REQUEST_SKILL_INSTALL_TOOL', 'ALL_SKILL_TOOLS', 'SKILL_TOOL_NAMES',
    'SKILL_INSTALL_TOOLS', 'SKILL_READ_TOOLS',
    'InstallerError', 'install_skill_package',
    'get_catalog', 'get_catalog_entry',
    'CatalogInstallError', 'install_catalog_skill',
    'render_skill_search', 'search_skill_catalog', 'search_skills',
    'OnlineCatalogError', 'install_clawhub_skill', 'search_online_skills',
]
