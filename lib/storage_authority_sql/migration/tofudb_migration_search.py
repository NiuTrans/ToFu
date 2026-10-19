"""Admit only engine-owned retired FTS index state after archiving its inputs.

The complete content and configuration remain durable inactive archives. The
virtual view and three index structures carry no additional logical documents.
SQL metadata belongs to the pinned source reader. Exact known DDL and SQLite's
virtual/shadow classification prevent a name-only disposable-table exception.
"""
from __future__ import annotations

POLICY = 'fts5-internal-index-with-archived-content-and-config'
_TABLE = 'conversations_fts'
_DEFINITIONS = {
    _TABLE: "CREATE VIRTUAL TABLE conversations_fts USING fts5(search_text, tokenize='unicode61')",
    _TABLE + '_config': "CREATE TABLE 'conversations_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID",
    _TABLE + '_content': "CREATE TABLE 'conversations_fts_content'(id INTEGER PRIMARY KEY, c0)",
    _TABLE + '_data': "CREATE TABLE 'conversations_fts_data'(id INTEGER PRIMARY KEY, block BLOB)",
    _TABLE + '_docsize': "CREATE TABLE 'conversations_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB)",
    _TABLE + '_idx': "CREATE TABLE 'conversations_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID",
}
# The virtual view plus three physical index tables; content/config are archived.
_DERIVED = (_TABLE, _TABLE + '_data', _TABLE + '_docsize', _TABLE + '_idx')


def verified_retired_search(source, plan):
    """Return rebuild evidence only for this exact archived-input FTS layout."""
    if plan.get('retiredSearchRebuildPolicy') != POLICY:
        return {}
    required = {
        'retired.conversations_fts_config': {
            'name': _TABLE + '_config', 'keys': ['k'], 'columns': ['k', 'v']},
        'retired_large.conversations_fts_content': {
            'name': _TABLE + '_content', 'keys': ['id'], 'columns': ['id', 'c0'],
            'retiredLarge': True},
    }
    domains = {domain['name']: domain for domain in plan['domains'] if 'name' in domain}
    for name, table in required.items():
        if domains.get(name, {}).get('tables') != [table]:
            return {}
    if not set(_DEFINITIONS).issubset(source.tables):
        return {}
    for name, expected in _DEFINITIONS.items():
        actual = source.table_definition(name)
        if (actual['kind'] != ('virtual' if name == _TABLE else 'shadow')
                or ' '.join(actual['sql'].split()).lower() != ' '.join(expected.split()).lower()):
            return {}
    return {name: {
        'proof': POLICY,
        'archivedInputTables': [_TABLE + '_content', _TABLE + '_config'],
        'action': 'reconstruct-from-archived-logical-rows;do-not-activate-retired-search',
    } for name in _DERIVED}
