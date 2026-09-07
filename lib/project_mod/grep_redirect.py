"""Runtime shell rewrite for filesystem grep segments in ``run_command``.

No search happens while this module plans a command.  A filesystem-reading
``grep`` token is replaced with an absolute, strictly-quoted helper command;
the original arguments, pipes, redirections, substitutions, subshells, and
control operators stay byte-for-byte where the model put them.  Consequently
``write file; grep file`` observes the newly-written bytes, and downstream
consumers such as ``head`` apply real shell backpressure.

The helper delegates to :mod:`lib.project_mod.grep_engine`, which streams
complete lines, preserves native GNU grep behaviour, and returns 124 with a
partial-results stderr note on its internal timeout.
"""

from __future__ import annotations

import os
import shlex
import sys
import time
from dataclasses import dataclass

from lib.log import get_logger
from lib.project_mod.command_analysis import (
    _REDIR_BARE_RE,
    _REDIR_FUSED_RE,
    _grep_segment_reads_filesystem,
    _shell_words,
    _split_pipeline_spans,
)
from lib.project_mod import grep_engine
from lib.project_mod.config import IGNORE_DIRS

logger = get_logger(__name__)

_GREP_BINARIES = frozenset({'grep', 'egrep', 'fgrep'})
_PREFIX_KEYWORDS = frozenset({'if', 'while', 'until', '!', 'time'})
_ENV_ASSIGN_RE = __import__('re').compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


@dataclass
class GrepRedirectPlan:
    rewritten: object = None
    refused_segment: object = None
    refusal_reason: object = None
    n_redirected: int = 0
    elapsed: float = 0.0


def _helper_replacement(program: str) -> str:
    """Return the shell-safe helper prefix that stands in for one grep word."""
    python = os.path.abspath(sys.executable)
    helper = os.path.abspath(grep_engine.__file__)
    parts = [
        shlex.quote(python),
        shlex.quote(helper),
        '--program',
        shlex.quote(program),
    ]
    for dirname in sorted(IGNORE_DIRS):
        parts.extend(('--exclude-dir', shlex.quote(dirname)))
    parts.append('--')
    return ' '.join(parts)


def _grep_argv_end(words, start, segment):
    """Index just past grep argv, before the first shell redirection."""
    for idx in range(start, len(words)):
        word = words[idx]
        raw = segment[word.start:word.end]
        if not word.squote and not word.dquote and (
                _REDIR_FUSED_RE.match(raw) or _REDIR_BARE_RE.match(raw)):
            return idx
    return len(words)


def plan_grep_redirect(command, cwd):
    """Rewrite filesystem grep binaries to the runtime helper.

    Returns ``None`` when the command has no direct filesystem grep.  GNU
    options are intentionally not parsed here: the native backend is the
    compatibility fallback for every option the fast public search path does
    not prove equivalent.
    """
    if os.name != 'posix' or not command or not command.strip():
        return None
    started = time.monotonic()
    replacements = []
    found_any = False

    for seg_start, seg_end in _split_pipeline_spans(command):
        segment = command[seg_start:seg_end]
        words = _shell_words(segment)
        if not words:
            continue
        # Keep shell keywords and environment assignments in place. Replacing
        # only the executable token works in ``if grep``, ``LC_ALL=C grep``,
        # command substitutions, and subshells without special group syntax.
        idx = 0
        while idx < len(words) and not words[idx].squote and not words[idx].dquote:
            text = words[idx].text
            if _ENV_ASSIGN_RE.match(text) or text in _PREFIX_KEYWORDS:
                idx += 1
                continue
            break
        if idx >= len(words):
            continue
        base = words[idx].text.split('/')[-1]
        if base in ('sudo', 'doas', 'builtin'):
            # Running the repository helper with elevated authority or through
            # the shell's builtin-only resolver is not a transparent rewrite.
            if any(w.text.split('/')[-1] in _GREP_BINARIES
                   for w in words[idx + 1:]):
                return GrepRedirectPlan(
                    refused_segment=segment.strip(),
                    refusal_reason=f'{base}-wrapped grep cannot be redirected')
            continue
        if base in ('command', 'exec') and idx + 1 < len(words):
            idx += 1
            base = words[idx].text.split('/')[-1]
        if base not in _GREP_BINARIES:
            continue
        argv_end = _grep_argv_end(words, idx, segment)
        grep_words = words[idx:argv_end]
        if any(w.text.startswith('<<') for w in grep_words):
            continue
        if not _grep_segment_reads_filesystem(grep_words):
            continue

        found_any = True
        program = words[idx].text
        absolute_start = seg_start + words[idx].start
        absolute_end = seg_start + words[idx].end
        replacements.append((
            absolute_start,
            absolute_end,
            _helper_replacement(program),
        ))

    if not found_any:
        return None
    rewritten = command
    for start, end, replacement in sorted(replacements, reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return GrepRedirectPlan(
        rewritten=rewritten,
        n_redirected=len(replacements),
        elapsed=time.monotonic() - started,
    )


__all__ = ['GrepRedirectPlan', 'plan_grep_redirect']
