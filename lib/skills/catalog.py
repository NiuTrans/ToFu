"""lib/skills/catalog.py — Curated catalog of well-known skill packages.

Mirrors the shape of ``lib/mcp/registry.py``: each entry describes a
downloadable skill package (Anthropic Skills, OpenClaw skills, team-specific
bundles) so the frontend can render a searchable App-Store grid.

An entry does NOT bundle the skill content \u2014 it only describes WHERE to
fetch it from (``download_url``, a ``.zip`` over HTTPS) plus the metadata
needed to render and install it. Install flow:

1. User clicks \u201cInstall\u201d on a card.
2. Backend streams the zip into a bounded in-memory buffer.
3. :func:`lib.skills.installer.install_skill_package` extracts it.

Adding entries requires an immutable HTTPS revision URL plus a canonical
selected-package SHA-256. Moving branch URLs are rejected at import time.
"""

from __future__ import annotations

import copy
import re
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
    subdir: str             # optional repo-relative path of the single
                            # sub-skill to install from a multi-skill
                            # archive (e.g. 'skills/pptx'). Without this,
                            # a mono-repo zip installs whichever SKILL.md
                            # the walker reaches first.
    source_revision: str    # immutable upstream commit/release identifier
    content_sha256: str     # canonical selected-package digest
    installable: bool       # false when product resource budgets reject it
    unavailable_reason: str


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


# Immutable archive identities. Keep revisions beside URLs so source review
# never has to trust an import-time rewrite of a moving branch.
_ANTHROPIC_REVISION = '3b3fad96af16a10759d930941b4520ba0c40edae'
_ANTHROPIC_ZIP = (
    f'https://codeload.github.com/anthropics/skills/zip/{_ANTHROPIC_REVISION}')
_OPENCLAW_REVISION = '73dab669ba7e293d162fad30620b05393ea9fc06'
_OPENCLAW_ZIP = (
    f'https://codeload.github.com/win4r/OpenClaw-Skill/zip/'
    f'{_OPENCLAW_REVISION}')
_FLYAI_REVISION = 'f89974d2bd4822e79cf16d1906c9c2a7c900f979'
_FLYAI_ZIP = (
    f'https://codeload.github.com/alibaba-flyai/flyai-skill/zip/'
    f'{_FLYAI_REVISION}')
_HYPERFRAMES_REVISION = '17ead629d010f7e5495f645d46fafd6876482c32'
_HYPERFRAMES_ZIP = (
    f'https://codeload.github.com/vibe-motion/auto-motion/zip/'
    f'{_HYPERFRAMES_REVISION}')
_SOURCE_REVISION_BY_URL = {
    _ANTHROPIC_ZIP: _ANTHROPIC_REVISION,
    _OPENCLAW_ZIP: _OPENCLAW_REVISION,
    _FLYAI_ZIP: _FLYAI_REVISION,
    _HYPERFRAMES_ZIP: _HYPERFRAMES_REVISION,
}


# \u2500\u2500 Curated Catalog \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
#
# Every download URL resolves to a zip with one unambiguous package root, or
# an entry supplies an exact ``subdir`` for selective extraction. GitHub
# codeload commit archives and immutable release assets are both supported.

CATALOG: list[SkillCatalogEntry] = [

    # \u2500\u2500 Anthropic official Skills \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    {
        'id': 'skill-creator',
        'name': 'Skill Creator',
        'description': 'Anthropic\u2019s scaffolding skill \u2014 lets the agent write new SKILL.md packages following best practices.',
        'icon': '\U0001f9ea',
        'category': CAT_CODE,
        'download_url': _ANTHROPIC_ZIP,
        'homepage': 'https://github.com/anthropics/skills/tree/main/skills/skill-creator',
        'subdir': 'skills/skill-creator',
        'author': 'Anthropic',
        'tags': ['anthropic', 'meta', 'authoring'],
    },
    {
        'id': 'docx-skill',
        'name': 'Word (docx)',
        'description': 'Create, read, and edit Word documents with full formatting \u2014 styles, tables, images.',
        'icon': '\U0001f4dd',
        'category': CAT_DOCS,
        'download_url': _ANTHROPIC_ZIP,
        'homepage': 'https://github.com/anthropics/skills/tree/main/skills/docx',
        'subdir': 'skills/docx',
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
        'download_url': _ANTHROPIC_ZIP,
        'homepage': 'https://github.com/anthropics/skills/tree/main/skills/xlsx',
        'subdir': 'skills/xlsx',
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
        'download_url': _ANTHROPIC_ZIP,
        'homepage': 'https://github.com/anthropics/skills/tree/main/skills/pdf',
        'subdir': 'skills/pdf',
        'author': 'Anthropic',
        'tags': ['pdf', 'document', 'extract'],
    },
    {
        'id': 'pptx-skill',
        'name': 'PowerPoint (pptx)',
        'description': 'Build, edit, and read PowerPoint decks \u2014 create from scratch (pptxgenjs) or from a template, with design-quality guidance and visual QA.',
        'icon': '\U0001f3a5',
        'category': CAT_DOCS,
        'download_url': _ANTHROPIC_ZIP,
        'homepage': 'https://github.com/anthropics/skills/tree/main/skills/pptx',
        'subdir': 'skills/pptx',
        'author': 'Anthropic',
        'tags': ['pptx', 'powerpoint', 'slides', 'deck', 'presentation', 'office'],
        'featured': True,
        'requires': {'bins': ['python3']},
        'install_note': 'Full features also use `pptxgenjs` (npm), `markitdown[pptx]` (pip) and LibreOffice for rendering.',
    },
    {
        'id': 'artifacts-builder',
        'name': 'Artifacts Builder',
        'description': 'Build polished Claude artifacts (HTML/React/SVG) with Anthropic\u2019s recommended layout patterns.',
        'icon': '\U0001f3a8',
        'category': CAT_CREATIVE,
        'download_url': _ANTHROPIC_ZIP,
        'homepage': 'https://github.com/anthropics/skills/tree/main/skills/web-artifacts-builder',
        'subdir': 'skills/web-artifacts-builder',
        'author': 'Anthropic',
        'tags': ['artifacts', 'html', 'react', 'svg'],
    },
    {
        'id': 'webapp-testing',
        'name': 'Web-app Testing',
        'description': 'Write end-to-end browser tests with Playwright inside a skill-driven workflow.',
        'icon': '\U0001f9ea',
        'category': CAT_CODE,
        'download_url': _ANTHROPIC_ZIP,
        'homepage': 'https://github.com/anthropics/skills/tree/main/skills/webapp-testing',
        'subdir': 'skills/webapp-testing',
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
        'download_url': _OPENCLAW_ZIP,
        'homepage': 'https://github.com/win4r/OpenClaw-Skill',
        'author': 'win4r (community)',
        'tags': ['openclaw', 'template', 'agentskills'],
    },

    # \u2500\u2500 Meituan internal (stripped by export.py opensource mode) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    # (none bundled by default \u2014 users can drag-drop citadel.zip /
    #  mlp-skills.zip or configure an internal registry via
    #  TOFU_SKILL_CATALOG_URL.)
]

# ── Local Life & Travel (China) ──────────────────────────────────────
#
# Vendors that ship a SKILL package rather than an MCP server. The MCP-shaped
# ones (Amap / RollingGo / 12306 / Tuniu) live in lib/mcp/registry.py under
# the same category name — the split follows the PROTOCOL, not the vendor.
#
# Admission criterion, same as the MCP catalog: a normal developer must be
# able to obtain credentials. Fliggy qualifies (self-service key, no company
# verification); Ctrip and Meituan do not — corporate onboarding only, and the
# owner decided 2026-07-27 not to pursue it (option C, ticket
#  CLOSED). The full rationale and the reopen condition live
# next to the MCP-side entries in lib/mcp/registry.py; do not add cards for
# them here either — test_no_dead_card_for_a_business_gated_vendor scans THIS
# catalogue too, precisely because a vendor may ship in either shape.

CATALOG += [
    {
        'id': 'flyai',
        'name': '飞猪 FlyAI（出行旅游）',
        'description': '阿里飞猪官方出行 skill：机票/火车/酒店/景点/演出的自然语言搜索，直连飞猪实时库存，结果自带可预订链接。零配置可用，填 API Key 后结果更完整。',
        'icon': '🐷',
        'category': CAT_PRODUCTIVITY,
        'download_url': _FLYAI_ZIP,
        'homepage': 'https://github.com/alibaba-flyai/flyai-skill',
        'subdir': 'skills/flyai',
        'author': 'Alibaba Fliggy',
        'tags': ['travel', 'china', 'flight', 'hotel', 'train', '机票', '酒店',
                 '火车票', '门票', '飞猪', '出行'],
        'featured': True,
        'requires': {'bins': ['node'], 'env': ['FLYAI_API_KEY']},
        'install_note': '八个搜索命令零配置即可用；如需更完整结果，在飞猪 AI 开放平台登录后自助领取 API Key(个人可申请，无需企业认证)，安装后在卡片上点「配置」填入。',
    },
]

# ── vibe-motion / HyperFrames video packs ────────────────────────────
#
# The six AgentSkills packs bundled by vibe-motion/auto-motion (the
# workflow tofu's motion-video pipeline absorbs — see
# docs/modules/ingest_media.md). All install from the same mono-repo zip;
# ``subdir`` picks the pack. The render toolchain itself (node /
# hyperframes CLI / ffmpeg / Chrome) is bootstrapped by the
# ``motion_video_env_check`` tool — these packs are pure KNOWLEDGE
# (composition contract, motion rules, design presets, workflow agents).

_HYPERFRAMES_SKILLS_PREFIX = 'exampleFolder/.claude/skills'

CATALOG += [
    {
        'id': 'hyperframes',
        'name': 'HyperFrames (router)',
        'description': 'HTML→video entry skill — the HyperFrames composition contract, CLI dev loop, and workflow intent router. Read-first for any video/animation task.',
        'icon': '🎬',
        'category': CAT_CREATIVE,
        'download_url': _HYPERFRAMES_ZIP,
        'homepage': 'https://github.com/vibe-motion/auto-motion',
        'subdir': f'{_HYPERFRAMES_SKILLS_PREFIX}/hyperframes',
        'author': 'vibe-motion',
        'tags': ['video', 'animation', 'hyperframes', 'motion'],
        'featured': True,
        'requires': {'bins': ['node', 'ffmpeg']},
        'install_note': 'Render toolchain is bootstrapped separately by the motion_video_env_check tool.',
    },
    {
        'id': 'hyperframes-motion',
        'name': 'HyperFrames Motion',
        'description': 'The motion knowledge pack: 29 atomic motion rules, 13 multi-phase scene blueprints with runnable examples, transitions, and per-runtime adapters (GSAP / Lottie / Three.js / WAAPI).',
        'icon': '✨',
        'category': CAT_CREATIVE,
        'download_url': _HYPERFRAMES_ZIP,
        'homepage': 'https://github.com/vibe-motion/auto-motion',
        'subdir': f'{_HYPERFRAMES_SKILLS_PREFIX}/hyperframes-motion',
        'author': 'vibe-motion',
        'tags': ['video', 'animation', 'motion', 'gsap', 'choreography'],
        'featured': True,
    },
    {
        'id': 'hyperframes-design',
        'name': 'HyperFrames Design',
        'description': 'Design direction for video scenes: 20+ frame presets, palettes, typography, beat planning and brand/style decisions.',
        'icon': '🎨',
        'category': CAT_CREATIVE,
        'download_url': _HYPERFRAMES_ZIP,
        'homepage': 'https://github.com/vibe-motion/auto-motion',
        'subdir': f'{_HYPERFRAMES_SKILLS_PREFIX}/hyperframes-design',
        'author': 'vibe-motion',
        'tags': ['video', 'design', 'palette', 'typography', 'brand'],
    },
    {
        'id': 'motion-graphics',
        'name': 'Motion Graphics (workflow)',
        'description': 'Director→Builder→Finalize subagent pipeline for short design-led motion graphics (kinetic type, stat count-ups, charts, logo stings, lower-thirds).',
        'icon': '📊',
        'category': CAT_CREATIVE,
        'download_url': _HYPERFRAMES_ZIP,
        'homepage': 'https://github.com/vibe-motion/auto-motion',
        'subdir': f'{_HYPERFRAMES_SKILLS_PREFIX}/motion-graphics',
        'author': 'vibe-motion',
        'tags': ['video', 'motion-graphics', 'kinetic-type', 'workflow'],
    },
    {
        'id': 'general-video',
        'name': 'General Video (workflow)',
        'description': 'Longer / multi-scene video workflow: design system → prompt expansion → plan → layout-before-animation → build → validate.',
        'icon': '📹',
        'category': CAT_CREATIVE,
        'download_url': _HYPERFRAMES_ZIP,
        'homepage': 'https://github.com/vibe-motion/auto-motion',
        'subdir': f'{_HYPERFRAMES_SKILLS_PREFIX}/general-video',
        'author': 'vibe-motion',
        'tags': ['video', 'workflow', 'multi-scene'],
    },
    {
        'id': 'vibe-image-gen',
        'name': 'Image Gen (MiniMax)',
        'description': 'Single-image generation from a text prompt via the MiniMax image-01 API (script + prompt-writing guide). Requires a MINIMAX_API_KEY; tofu deployments usually prefer the built-in generate_image tool.',
        'icon': '🖼️',
        'category': CAT_CREATIVE,
        'download_url': _HYPERFRAMES_ZIP,
        'homepage': 'https://github.com/vibe-motion/auto-motion',
        'subdir': f'{_HYPERFRAMES_SKILLS_PREFIX}/image-gen',
        'author': 'vibe-motion',
        'tags': ['image', 'generation', 'minimax', 'assets'],
        'requires': {'env': ['MINIMAX_API_KEY']},
        'install_note': 'The SKILL.md-derived install id is "image-gen" (upstream name); this catalog entry tracks it via the .catalog_id marker.',
    },
]


# \u2500\u2500 Lookup helpers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

_CONTENT_SHA256_BY_ID = {
    'skill-creator': 'c6ba7c16cc662ffd7cfe81a66079cf7376e88f7f0f51094da214fae30d3af032',
    'docx-skill': 'b863c828a5272ffd5b2fd9fc6cbd8004217fdc42cb050c5015c0881e9d3023af',
    'xlsx-skill': '263d3e7635ea8c1b4133acd5fd1add8b9f8897f1f75ea9592953d3fe6045ac37',
    'pdf-skill': 'd9a070202fe59ea9c65c4ae5a5fc0839028c73bea0dd0826baa5ae570e2fc597',
    'pptx-skill': '7d0814e808b46791436f59865f4b6fcba044562619e93b71fb2e32618080af8c',
    'artifacts-builder': 'aba441b53ea102937082c912ef572d4d2a18441d56fdae6fbbcb90a35ef56a3f',
    'webapp-testing': '33030bf51f9747af04995b9bcc22387b77d229bd341514cd9cfbb91cf1ce64d1',
    'openclaw-skill-starter': 'b4d0d841ecb1fc559a61eba3c40215a3a38fa98d40ca35416ab96ffb8a5ff09e',
    'flyai': 'baa0ed6a37044afe73db0466ce18933628a6a581cc1f0cd4384ef86c18f09c9e',
    'hyperframes': 'b27ac4a7c4ca7aeca6c99c04b1a83d2ef5b458ae3535c76a070834a9aff0784b',
    'hyperframes-motion': '331b487d39fcbc3de2432b8c5fad780d2abaf2a99b268f0d733bedc47cad7b48',
    'hyperframes-design': '5bf3229d59cb75df79587d7e6764bd9b953242f962afc5b2cf45a725f35606dc',
    'motion-graphics': '322d75b3c4336ce232253bb0ab82ab7828466e69ea57bed7a2cd11e509ec9914',
    'general-video': 'c107cf7cb9d99db68c6cfeeb8be99c335b499a6b00db3077bb0541ce3bcf92ba',
    'vibe-image-gen': 'e9af29a0a2643d951cb67d32f4fb9a74e6891f917098d54fda6bfe8faae4deba',
}


def _seal_catalog() -> None:
    """Convert human-authored cards into immutable install identities."""
    seen: set[str] = set()
    for entry in CATALOG:
        skill_id = str(entry.get('id') or '')
        if not skill_id or skill_id in seen:
            raise RuntimeError(
                f'invalid or duplicate skill catalog id: {skill_id!r}')
        seen.add(skill_id)
        url = str(entry.get('download_url') or '')
        revision = _SOURCE_REVISION_BY_URL.get(url, '')
        digest = _CONTENT_SHA256_BY_ID.get(skill_id, '')
        if (not re.fullmatch(r'[0-9a-f]{40}', revision)
                or revision not in url
                or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise RuntimeError(f'unsealed skill catalog entry: {skill_id}')
        entry['source_revision'] = revision
        entry['content_sha256'] = digest
        entry.setdefault('installable', True)

    # This selected package is about 40 MiB unpacked. Keep the 25 MiB
    # personal-computer budget instead of silently granting one exception.
    oversized = next(
        entry for entry in CATALOG if entry['id'] == 'hyperframes-motion')
    oversized['installable'] = False
    oversized['unavailable_reason'] = (
        'Selected package exceeds the 25 MiB installed-skill budget.')


_seal_catalog()

# Runtime installation reads a sealed copy rather than the human-authored
# module list. Even an accidental in-process mutation of exported ``CATALOG``
# cannot retarget a catalog id after startup.
_SEALED_CATALOG: tuple[SkillCatalogEntry, ...] = tuple(
    copy.deepcopy(entry) for entry in CATALOG)
_CATALOG_INDEX: dict[str, SkillCatalogEntry] = {
    entry['id']: copy.deepcopy(entry) for entry in _SEALED_CATALOG
}


def get_catalog() -> list[SkillCatalogEntry]:
    """Return request-owned values so annotations cannot mutate globals."""
    return [copy.deepcopy(entry) for entry in _SEALED_CATALOG]


def get_catalog_entry(skill_id: str) -> SkillCatalogEntry | None:
    entry = _CATALOG_INDEX.get(skill_id)
    return copy.deepcopy(entry) if entry is not None else None
