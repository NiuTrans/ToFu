"""Render every accepted definition in a ranked repository map.

Line numbers are 1-based; files are best-first and definitions stay in source
order. Selection/admission belongs to scan; model output sizing belongs to
shared canonical capture and L0, never this producer.
"""
from __future__ import annotations

from .outline import RepoMap

_CHARS_PER_TOKEN = 3  # Statistics only, not an output budget.


def render_map(repo: RepoMap,
               file_scores: dict[str, float],
               chat_rel_paths: set[str] | None = None) -> tuple[str, dict]:
    """Return the complete selected outline and observational statistics."""
    chat = chat_rel_paths or set()
    scored = [
        (score + (1e-9 if path in chat else 0.0), path)
        for path, score in file_scores.items() if repo.tags.get(path)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))
    blocks = []
    tags_shown = 0
    for _score, path in scored:
        # Preserve exact scan identity while keeping model/event output UTF-8.
        display_path = path
        if not path.isprintable() or '\\' in path or '"' in path:
            import json
            display_path = json.dumps(path, ensure_ascii=True)
        lines = [f'{display_path}:']
        for tag in repo.tags[path]:
            pad = '  ' * (tag.indent + 1)
            lines.append(f'{pad}{tag.line}: {tag.signature or tag.name}')
            tags_shown += 1
        blocks.append('\n'.join(lines))
    text = '\n\n'.join(blocks) + '\n' if blocks else ''
    return text, {
        'files_total': len(scored),
        'files_shown': len(scored),
        'tags_shown': tags_shown,
        'est_tokens': (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN,
        'omitted': 0,
    }
