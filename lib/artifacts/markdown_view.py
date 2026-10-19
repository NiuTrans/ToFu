"""Shared inert Markdown body rendering for artifact preview and PDF export.

Entry: render_markdown_html. MarkdownIt loads on demand; unavailable rendering
falls back to escaped source text. Neither path executes model-supplied HTML.
"""
from html import escape


def render_markdown_html(content: str) -> str:
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return '<pre>' + escape(content or '') + '</pre>'
    renderer = MarkdownIt('commonmark', {
        'breaks': True, 'html': False, 'linkify': True, 'typographer': False,
    })
    return renderer.render(content or '')
