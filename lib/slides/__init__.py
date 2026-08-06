"""lib/slides — the slides capability: one sentence → an editable PPTX.

(docs/SLIDES_CAPABILITY_DESIGN.md §4). PPTD (YAML DSL) is the authoring
layer; ``render_html``/``render_png`` are the preview ground truth;
``export_pptx`` writes native editable OOXML; ``recipe`` + ``engine`` +
``runtime`` ride the production substrate. Lazy PEP 562 facade, same
discipline as lib/agent_core and lib/design_sys.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

CORE_MEMBERS = {
    # pptd
    'PPTDError': 'pptd',
    'Deck': 'pptd',
    'Page': 'pptd',
    'parse_deck': 'pptd',
    'validate_deck': 'pptd',
    'resolve_color': 'pptd',
    # render
    'render_page_html': 'render_html',
    'render_deck_html': 'render_html',
    'render_previews': 'render_png',
    'render_page_png': 'render_png',
    # export
    'export_pptx': 'export_pptx',
    'ExportError': 'export_pptx',
    # authoring
    'author_page': 'author',
    'fallback_page': 'author',
    # recipe/engine
    'build_deck_from_topic': 'recipe',
    'slides_recipe_stages': 'recipe',
    'start_slides_job': 'engine',
    'run_slides_task': 'engine',
    'resume_interrupted_decks': 'engine',
    'slides_root': 'engine',
}

__all__ = ['CORE_MEMBERS']

_cache: dict = {}


def __getattr__(name: str):
    module = CORE_MEMBERS.get(name)
    if module is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    if name not in _cache:
        import importlib
        _cache[name] = getattr(importlib.import_module(f'.{module}', __name__),
                               name)
    return _cache[name]


def __dir__():
    return sorted(set(globals()) | set(CORE_MEMBERS))
