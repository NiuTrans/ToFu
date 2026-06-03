"""lib/memory/catalog.py \u2014 Curated catalog of well-known skill packages.

Mirrors the shape of ``lib/mcp/registry.py``: each entry describes a
downloadable skill package (Anthropic Skills, OpenClaw skills, team-specific
bundles) so the frontend can render a searchable App-Store grid.

An entry does NOT bundle the skill content \u2014 it only describes WHERE to
fetch it from (``download_url``, a ``.zip`` over HTTPS) plus the metadata
needed to render and install it. Install flow:

1. User clicks \u201cInstall\u201d on a card.
2. Backend downloads the zip to memory (bounded by
   :data:`lib.memory.installer._MAX_BYTES`).
3. :func:`lib.memory.installer.install_skill_package` extracts it.

Adding entries: append to :data:`CATALOG` at the bottom of this file. Only
``id`` / ``name`` / ``description`` / ``download_url`` are required.
"""

from __future__ import annotations

from typing import TypedDict

from lib.log import get_logger

logger = get_logger(__name__)


# \u2500\u2500 Types \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

class SkillCatalogEntry(TypedDict, total=False):
    id: str                 # canonical id (also the installed folder name)
    name: str               # display name
    description: str        # one-liner for card
    icon: str               # emoji or single-line inline SVG
    category: str           # for grouping
    download_url: str       # HTTPS .zip to fetch on install
    homepage: str           # docs / repo link
    tags: list[str]
    featured: bool
    author: str             # display author (e.g. "Anthropic")
    requires: dict          # optional {bins: [...], env: [...]} hint
    install_note: str       # optional sentence shown under the card
    docs_path: str          # optional path inside the zip to link on card


# \u2500\u2500 Categories \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

CAT_DOCS = 'Documents'
CAT_CODE = 'Coding'
CAT_CREATIVE = 'Creative'
CAT_INFRA = 'Infrastructure'
CAT_PRODUCTIVITY = 'Productivity'
CAT_RESEARCH = 'Research'
CAT_OTHER = 'Other'

CATEGORIES = [
    CAT_DOCS, CAT_CODE, CAT_CREATIVE, CAT_INFRA,
    CAT_PRODUCTIVITY, CAT_RESEARCH, CAT_OTHER,
]


# \u2500\u2500 Curated Catalog \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
#
# All download URLs should point to a `.zip` that unzips to either
# `<name>/SKILL.md` or `<name>/<name>/SKILL.md` (both wrappers auto-handled
# by the installer).  GitHub release assets and `codeload.github.com`
# refs are both fine.

CATALOG: list[SkillCatalogEntry] = [

    # \u2500\u2500 Anthropic official Skills \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    {
        'id': 'anthropic-skills',
        'name': 'Anthropic Skills (all)',
        'description': 'Official Claude Skills bundle \u2014 docx, xlsx, pptx, pdf, Artifacts builder, webapp-testing, Skill-Creator, and more.',
        'icon': '\U0001f9e1',
        'category': CAT_DOCS,
        'download_url': 'https://github.com/anthropics/skills/archive/refs/heads/main.zip',
        'homepage': 'https://github.com/anthropics/skills',
        'author': 'Anthropic',
        'tags': ['official', 'anthropic', 'claude', 'docs', 'artifacts', 'pdf', 'excel'],
        'featured': True,
        'install_note': 'Installs the whole repo; sub-skills live under skills/*/SKILL.md.',
    },
    {
        'id': 'skill-creator',
        'name': 'Skill Creator',
        'description': 'Anthropic\u2019s scaffolding skill \u2014 lets the agent write new SKILL.md packages following best practices.',
        'icon': '\U0001f9ea',
        'category': CAT_CODE,
        'download_url': 'https://codeload.github.com/anthropics/skills/zip/refs/heads/main',
        'homepage': 'https://github.com/anthropics/skills/tree/main/skill-creator',
        'author': 'Anthropic',
        'tags': ['anthropic', 'meta', 'authoring'],
    },
    {
        'id': 'docx-skill',
        'name': 'Word (docx)',
        'description': 'Create, read, and edit Word documents with full formatting \u2014 styles, tables, images.',
        'icon': '\U0001f4dd',
        'category': CAT_DOCS,
        'download_url': 'https://codeload.github.com/anthropics/skills/zip/refs/heads/main',
        'homepage': 'https://github.com/anthropics/skills/tree/main/document-skills/docx',
        'author': 'Anthropic',
        'tags': ['word', 'docx', 'document', 'office'],
        'featured': True,
        'requires': {'bins': ['python3']},
    },
    {
        'id': 'xlsx-skill',
        'name': 'Excel (xlsx)',
        'description': 'Read and write Excel workbooks with formulas, charts, and conditional formatting.',
        'icon': '\U0001f4ca',
        'category': CAT_DOCS,
        'download_url': 'https://codeload.github.com/anthropics/skills/zip/refs/heads/main',
        'homepage': 'https://github.com/anthropics/skills/tree/main/document-skills/xlsx',
        'author': 'Anthropic',
        'tags': ['excel', 'xlsx', 'spreadsheet', 'office'],
        'featured': True,
        'requires': {'bins': ['python3']},
    },
    {
        'id': 'pdf-skill',
        'name': 'PDF',
        'description': 'Extract, annotate, and generate PDFs with forms and tables preserved.',
        'icon': '\U0001f4c4',
        'category': CAT_DOCS,
        'download_url': 'https://codeload.github.com/anthropics/skills/zip/refs/heads/main',
        'homepage': 'https://github.com/anthropics/skills/tree/main/document-skills/pdf',
        'author': 'Anthropic',
        'tags': ['pdf', 'document', 'extract'],
    },
    {
        'id': 'pptx-skill',
        'name': 'PowerPoint (pptx)',
        'description': 'Build and edit PowerPoint decks \u2014 slides, layouts, speaker notes.',
        'icon': '\U0001f3a5',
        'category': CAT_DOCS,
        'download_url': 'https://codeload.github.com/anthropics/skills/zip/refs/heads/main',
        'homepage': 'https://github.com/anthropics/skills/tree/main/document-skills/pptx',
        'author': 'Anthropic',
        'tags': ['pptx', 'powerpoint', 'slides', 'office'],
    },
    {
        'id': 'artifacts-builder',
        'name': 'Artifacts Builder',
        'description': 'Build polished Claude artifacts (HTML/React/SVG) with Anthropic\u2019s recommended layout patterns.',
        'icon': '\U0001f3a8',
        'category': CAT_CREATIVE,
        'download_url': 'https://codeload.github.com/anthropics/skills/zip/refs/heads/main',
        'homepage': 'https://github.com/anthropics/skills/tree/main/artifacts-builder-skill',
        'author': 'Anthropic',
        'tags': ['artifacts', 'html', 'react', 'svg'],
    },
    {
        'id': 'webapp-testing',
        'name': 'Web-app Testing',
        'description': 'Write end-to-end browser tests with Playwright inside a skill-driven workflow.',
        'icon': '\U0001f9ea',
        'category': CAT_CODE,
        'download_url': 'https://codeload.github.com/anthropics/skills/zip/refs/heads/main',
        'homepage': 'https://github.com/anthropics/skills/tree/main/webapp-testing-skill',
        'author': 'Anthropic',
        'tags': ['playwright', 'testing', 'browser'],
        'requires': {'bins': ['node'], 'env': []},
    },

    # \u2500\u2500 OpenClaw-flavoured open-source examples \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    {
        'id': 'openclaw-skill-starter',
        'name': 'OpenClaw Skill Starter',
        'description': 'Reference skill package demonstrating OpenClaw AgentSkills format (metadata gating, installer specs).',
        'icon': '\U0001f43e',
        'category': CAT_CODE,
        'download_url': 'https://codeload.github.com/win4r/OpenClaw-Skill/zip/refs/heads/main',
        'homepage': 'https://github.com/win4r/OpenClaw-Skill',
        'author': 'win4r (community)',
        'tags': ['openclaw', 'template', 'agentskills'],
    },

    # \u2500\u2500 Meituan internal (stripped by export.py opensource mode) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    # (none bundled by default \u2014 users can drag-drop citadel.zip /
    #  mlp-skills.zip or configure an internal registry via
    #  TOFU_SKILL_CATALOG_URL.)
]


# \u2500\u2500 Lookup helpers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

_CATALOG_INDEX: dict[str, SkillCatalogEntry] = {e['id']: e for e in CATALOG}


def get_catalog() -> list[SkillCatalogEntry]:
    """Return the full curated catalog."""
    return list(CATALOG)


def get_catalog_entry(skill_id: str) -> SkillCatalogEntry | None:
    return _CATALOG_INDEX.get(skill_id)
