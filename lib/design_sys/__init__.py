"""lib/design_sys — Shared design system for every visual capability.

The layer that decides what a produced frame / slide LOOKS like, consumed by
both motion_video and slides (docs/SLIDES_CAPABILITY_DESIGN.md §3). Four
members, each deliberately capability-agnostic:

  * ``fonts``      — the curated, license-vetted typeface registry. Every face
                     rides the content-addressed asset store and is declared
                     scene-locally with ``@font-face`` (never fontconfig).
  * ``themes``     — per-scenario design bibles + palette/font-pair tokens, so
                     one film/deck carries ONE theme from end to end instead
                     of a per-scene colour roulette.
  * ``imagery``    — real-photo search / generation into the same asset store.
  * ``visual_qa``  — the multimodal hold-out: screenshot → checklist review →
                     structured findings (never a retry loop by itself; the
                     owning capability decides how to repair).

Lazy PEP 562 facade (same discipline as lib/agent_core): importing the package
costs nothing; each submodule loads on first attribute access.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['CORE_MEMBERS']

#: symbol → submodule. Mirrors the agent_core facade contract.
CORE_MEMBERS = {
    # fonts
    'FontFace': 'fonts',
    'FONT_REGISTRY': 'fonts',
    'get_font': 'fonts',
    'list_fonts': 'fonts',
    'get_pairing': 'fonts',
    'ensure_font': 'fonts',
    'stage_font_into_scene': 'fonts',
    'font_face_block': 'fonts',
    'LICENSES': 'fonts',
    # themes
    'Theme': 'themes',
    'BIBLE_INDEX': 'themes',
    'SCENARIOS': 'themes',
    'get_theme': 'themes',
    'list_themes': 'themes',
    'classify_scenario': 'themes',
    'theme_prompt_block': 'themes',
    'design_bible_text': 'themes',
    # imagery
    'search_photo': 'imagery',
    'photo_into_scene': 'imagery',
    'IMAGERY_LICENSE_NOTE': 'imagery',
    # visual_qa
    'visual_qa': 'visual_qa',
    'qa_frame': 'visual_qa',
    'QA_CHECKLIST': 'visual_qa',
    'visual_qa_available': 'visual_qa',
}

_members_cache: dict = {}


def __getattr__(name: str):
    module = CORE_MEMBERS.get(name)
    if module is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    if name not in _members_cache:
        import importlib
        _members_cache[name] = getattr(
            importlib.import_module(f'.{module}', __name__), name)
    return _members_cache[name]


def __dir__():
    return sorted(set(globals()) | set(CORE_MEMBERS))
