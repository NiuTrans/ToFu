"""The two saved search URL lists: operator bans and fetch-quality skips.

Responsibility: read and write the lists persisted in the ``search`` section
of ``server_config.json``. They carry different intents and are enforced
differently:

* ``skip_domains`` — operator bans. No tool may reach a matching URL: search
  listings, fetches, browser navigation and ``run_command`` transfers are all
  refused (:mod:`lib.search.url_access`, tofu-search). Written by the Settings
  UI, the ``update_search_settings`` tool and evaluation configs.
* ``fetch_skip_domains`` — fetch-quality skips written by the Daily Optimizer
  for domains whose pages keep proving irrelevant. tofu-search spends no
  anonymous fetch on them, but they stay in search listings and every other
  tool may open them. tofu-search also skips built-in hosts whose pages yield
  no extractable text (``tofu_search.fetch.utils.NO_TEXT_DOMAINS``); those are
  never stored here.

A ``skip_domains`` list without ``skip_domains_schema`` (schema 1) predates
that split: it was the fetcher's skip list, and every writer seeded it with
the fetcher's nine built-in hosts, so those entries were defaults rather than
bans. Reading schema 1 drops exactly those nine; the next write stores
schema 2, after which every entry is honoured as written.

Entry points: :func:`saved_skip_domains`, :func:`store_skip_domains`,
:func:`saved_fetch_skip_domains`.
Dependencies: standard library only — ``lib/__init__`` reads both lists at
import time.
"""

from __future__ import annotations

SKIP_DOMAINS_SCHEMA_KEY = 'skip_domains_schema'
SKIP_DOMAINS_SCHEMA = 2

# The fetcher's built-in skip hosts exactly as schema-1 writers seeded them.
# Frozen history: changing tofu-search's NO_TEXT_DOMAINS must not change it.
_SCHEMA_1_SEEDED_HOSTS = frozenset({
    'youtube.com', 'youtu.be', 'twitter.com', 'x.com',
    'facebook.com', 'instagram.com', 'tiktok.com',
    'linkedin.com', 'discord.com',
})


def _saved_list(search_section, key) -> set:
    entries = search_section.get(key) if isinstance(search_section, dict) else None
    return set(entries) if isinstance(entries, list) else set()


def saved_skip_domains(search_section) -> set:
    """Operator bans saved in a ``search`` section (empty when none are saved)."""
    entries = _saved_list(search_section, 'skip_domains')
    if entries and search_section.get(SKIP_DOMAINS_SCHEMA_KEY) != SKIP_DOMAINS_SCHEMA:
        entries -= _SCHEMA_1_SEEDED_HOSTS
    return entries


def store_skip_domains(search_section: dict, entries) -> list:
    """Store operator bans in ``search_section`` under the current schema."""
    stored = sorted(set(entries))
    search_section['skip_domains'] = stored
    search_section[SKIP_DOMAINS_SCHEMA_KEY] = SKIP_DOMAINS_SCHEMA
    return stored


def saved_fetch_skip_domains(search_section) -> set:
    """Fetch-quality skips saved in a ``search`` section (empty when none are saved)."""
    return _saved_list(search_section, 'fetch_skip_domains')


__all__ = [
    'SKIP_DOMAINS_SCHEMA', 'SKIP_DOMAINS_SCHEMA_KEY',
    'saved_fetch_skip_domains', 'saved_skip_domains', 'store_skip_domains',
]
