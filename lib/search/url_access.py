"""Host-side application of the ``search.skip_domains`` URL blocklist (operator bans).

Responsibility: the single refusal message the browser tools and the
pre-tool hook return when a tool call names a blocked URL. The matching rule
lives in :mod:`lib.search.url_blocklist`; the tofu-search package uses its generated
copy, so server-side fetches, browser navigation and shell transfers cannot
disagree about one URL.

Entry points: :func:`blocked_url_message`, :func:`blocked_text_message`,
:func:`url_argument_blocklist_hook` (registered by ``tool_hooks``).
Dependencies: ``lib`` (the saved setting), ``lib.search.url_blocklist``.
"""

from __future__ import annotations

import re

from lib.search.url_blocklist import blocked_url

# Absolute http(s) URLs inside a shell command or a tool argument. Stops at
# shell whitespace, quotes and the common closing delimiters, so a URL written
# in a Python string or a JSON array is still found without swallowing syntax.
_URL_TOKEN = re.compile(r'https?://[^\s"\'<>`)\]},;]+')


def _entries():
    import lib
    return getattr(lib, 'SKIP_DOMAINS', ()) or ()


def blocked_url_message(url: str) -> str | None:
    """The refusal to return for a blocked URL, or ``None`` when it is allowed."""
    entry = blocked_url(url, _entries())
    if entry is None:
        return None
    return (
        f'Error: URL blocked by the search.skip_domains setting (entry {entry!r}): {url}\n'
        'The operator configured this source as off-limits for Tofu\'s web tools; '
        'do not fetch it by another transport either. Remove the entry in '
        'Settings → Search if the block is unintended. Work without it, or use '
        'a different, unblocked source.'
    )


def blocked_text_message(text: str) -> str | None:
    """Refusal for the first blocked absolute URL appearing in free text."""
    for match in _URL_TOKEN.finditer(text or ''):
        refusal = blocked_url_message(match.group(0))
        if refusal:
            return refusal
    return None


def url_argument_blocklist_hook(tool_name: str, args: dict, task: dict):
    """Pre-tool hook: refuse tool calls whose arguments name a banned URL.

    Covers the paths a fetch-only gate misses: ``run_command`` (curl, wget,
    ``python -c urlopen``, ``git clone``) and the browser tools that navigate
    rather than read. An absolute URL anywhere in a command counts: the hook
    cannot tell a transfer from a mention (``echo``, a commit message, a
    string handed to a parser), which is acceptable only because every entry
    is an operator ban — fetch-quality skips never reach this list
    (:mod:`lib.search.saved_domain_lists`). A command that merely mentions a
    host name is not refused, so local files and directory names keep working.
    """
    if tool_name not in _GUARDED_TOOLS:
        return None
    text = ' '.join(str(args.get(key) or '') for key in _GUARDED_TOOLS[tool_name])
    refusal = blocked_text_message(text)
    if refusal is None:
        return None
    from lib.tasks_pkg.tool_hooks import HookResult
    return HookResult(action='block', message=refusal,
                      additional_context=(
                          'Re-issue without the blocked URL; other internet '
                          'access and the rest of this command\'s work are '
                          'unaffected. Do not retry the identical call.'))


_GUARDED_TOOLS = {
    'run_command': ('command',),
    'browser_navigate': ('url',),
    'browser_preview_page': ('url',),
    'browser_research_page': ('url',),
    'browser_read_page': ('url',),
    'fetch_url': ('url', 'urls'),
    'browser_download_url_to_server': ('url',),
}


__all__ = ['blocked_text_message', 'blocked_url_message', 'url_argument_blocklist_hook']
