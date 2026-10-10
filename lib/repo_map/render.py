"""lib/repo_map/render.py — Turn a ranked RepoMap into token-bounded text.

Output shape (matches what aider feeds models; familiar to LLM pretraining):

    lib/storage/lock.py:
      8: class FileLease(path, ttl)
     42:   def acquire(self, timeout=30)
     61:   def release(self)

    lib/agent_core/profiles.py:
      65: _BUILTINS = {...}

    (12 more files with definitions not shown — raise max_tokens to include)

Properties:
  * Line numbers are 1-based so the model can immediately hand them to
    read_files(path, start_line=…).
  * Files are emitted best-first; when the budget would be exceeded we drop
    the whole tail of files rather than truncating a file mid-block — a
    partial block misleads more than an absent one.
  * Within a file, tags stay in source order so the map doubles as a
    skim-ready skeleton of that file.
"""
from __future__ import annotations

from .outline import RepoMap

# tiktoken's o200k/cl100k average on source code is slightly above 3
# chars/token. We budget with 3 (conservative) so we never overshoot.
_CHARS_PER_TOKEN = 3


def render_map(repo: RepoMap,
               file_scores: dict[str, float],
               max_tokens: int = 2048,
               chat_rel_paths: set[str] | None = None) -> tuple[str, dict]:
    """Render the map. Returns (text, stats).

    ``stats``: files_total, files_shown, tags_shown, est_tokens, omitted.
    """
    max_chars = max(256, max_tokens) * _CHARS_PER_TOKEN
    # Each file gets at most this many lines — enough for orientation
    # (the top-level classes + entry functions), never enough for a dump.
    per_file_tags = 12
    chat = chat_rel_paths or set()

    # Files that have at least one definition, best first. Chat files get a
    # tiny boost to break ties toward what the user is editing right now.
    scored = [
        (score + (1e-9 if p in chat else 0.0), p)
        for p, score in file_scores.items()
        if repo.tags.get(p)
    ]
    scored.sort(key=lambda kv: (-kv[0], kv[1]))

    lines: list[str] = []
    used = 0
    files_shown = 0
    tags_shown = 0

    def _emit_file(rel_path: str, tags) -> str:
        """Format one file's block, capped at ``per_file_tags`` definitions."""
        block_lines = [f'{rel_path}:']
        shown = tags[:per_file_tags]
        for tag in shown:
            pad = '  ' * (tag.indent + 1)
            sig = tag.signature or tag.name
            block_lines.append(f'{pad}{tag.line}: {sig}')
        remaining = len(tags) - len(shown)
        if remaining > 0:
            block_lines.append(f'  ... ({remaining} more definitions)')
        return '\n'.join(block_lines) + '\n\n'

    for _score, rel_path in scored:
        tags = repo.tags[rel_path]
        block = _emit_file(rel_path, tags)
        if used + len(block) > max_chars:
            if files_shown == 0:
                lines.append(block)
                used += len(block)
                files_shown += 1
                tags_shown += min(len(tags), per_file_tags)
            break
        lines.append(block)
        used += len(block)
        files_shown += 1
        tags_shown += min(len(tags), per_file_tags)

    files_total = len([p for p in file_scores if repo.tags.get(p)])
    omitted = files_total - files_shown
    body = ''.join(lines).rstrip() + '\n' if lines else ''
    footer = ''
    if omitted > 0:
        plural = 's' if omitted != 1 else ''
        footer = (f'\n({omitted} more file{plural} with definitions not '
                  f'shown — raise max_tokens to include them)\n')

    text = body + footer
    stats = {
        'files_total': files_total,
        'files_shown': files_shown,
        'tags_shown': tags_shown,
        'est_tokens': (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN,
        'omitted': omitted,
    }
    return text, stats
