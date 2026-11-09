"""search.skip_domains holds operator bans only; fetch-quality skips live apart.

Executable specification for lib/search/saved_domain_lists.py and the
import-time lists on ``lib``:

  * a list saved before the ban/fetch-skip split (no ``skip_domains_schema``)
    is read without the nine hosts every writer seeded as defaults;
  * a stamped (schema 2) list is honoured as written, those hosts included;
  * a saved list equal to the old defaults no longer refuses a
    ``run_command`` transfer or browser navigation to those hosts, while an
    operator entry still does (the TikTok refusal reported 2026-11-08).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib.search.saved_domain_lists import (
    SKIP_DOMAINS_SCHEMA,
    SKIP_DOMAINS_SCHEMA_KEY,
    saved_fetch_skip_domains,
    saved_skip_domains,
    store_skip_domains,
)

pytestmark = pytest.mark.unit

SEEDED_DEFAULTS = ['discord.com', 'facebook.com', 'instagram.com', 'linkedin.com',
                   'tiktok.com', 'twitter.com', 'x.com', 'youtu.be', 'youtube.com']


def test_schema_1_list_drops_only_the_seeded_hosts():
    section = {'skip_domains': SEEDED_DEFAULTS + ['marginlab.ai', 'www.youtube.com']}
    assert saved_skip_domains(section) == {'marginlab.ai', 'www.youtube.com'}


def test_schema_2_list_is_honoured_as_written():
    section = {'skip_domains': ['x.com', 'marginlab.ai'],
               SKIP_DOMAINS_SCHEMA_KEY: SKIP_DOMAINS_SCHEMA}
    assert saved_skip_domains(section) == {'x.com', 'marginlab.ai'}


@pytest.mark.parametrize('section', [None, {}, {'gateway': {}},
                                     {'skip_domains': 'x.com', 'fetch_skip_domains': 'x.com'}])
def test_missing_or_malformed_lists_hold_no_entries(section):
    assert saved_skip_domains(section) == set()
    assert saved_fetch_skip_domains(section) == set()


def test_store_stamps_the_schema_and_keeps_other_keys():
    section = {'skip_domains': SEEDED_DEFAULTS, 'gateway': {'enabled': False}}
    assert store_skip_domains(section, saved_skip_domains(section) | {'x.com'}) == ['x.com']
    assert section == {'skip_domains': ['x.com'], 'gateway': {'enabled': False},
                       SKIP_DOMAINS_SCHEMA_KEY: SKIP_DOMAINS_SCHEMA}
    assert saved_skip_domains(section) == {'x.com'}


def test_fetch_skips_are_read_as_written():
    section = {'fetch_skip_domains': ['junk.example', 'x.com']}
    assert saved_fetch_skip_domains(section) == {'junk.example', 'x.com'}


def test_seeded_saved_list_no_longer_refuses_shell_or_browser_access(tmp_path):
    """The reported case, through the real import-time read and pre-tool hook."""
    base = tmp_path / 'Tofu'
    (base / 'data' / 'config').mkdir(parents=True)
    (base / 'data' / 'config' / 'server_config.json').write_text(json.dumps({'search': {
        'skip_domains': SEEDED_DEFAULTS + ['marginlab.ai'],
        'fetch_skip_domains': ['junk.example']}}))
    repo = Path(__file__).resolve().parents[2]
    program = (
        'import sys; sys.path.insert(0, %r); ' % str(repo)
        + 'import lib; from lib.search.url_access import url_argument_blocklist_hook as hook; '
        "shell = hook('run_command', {'command': 'curl -sSL https://www.tiktok.com/@954369920'}, {}); "
        "browser = hook('browser_navigate', {'url': 'https://www.youtube.com/watch?v=x'}, {}); "
        "banned = hook('run_command', {'command': 'curl -sSL https://marginlab.ai/x'}, {}); "
        'print(lib.__file__); '
        'print(sorted(lib.SKIP_DOMAINS), sorted(lib.FETCH_SKIP_DOMAINS)); '
        'print(shell is None, browser is None, banned is not None and banned.action)')
    environment = {'TOFU_DATA_DIR': str(base), 'HOME': str(tmp_path),
                   'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
    probe = subprocess.run([sys.executable, '-I', '-c', program], env=environment,
                           cwd=tmp_path, capture_output=True, text=True, check=False)
    assert probe.returncode == 0, probe.stdout[-2000:] + probe.stderr[-3000:]
    module_path, lists, verdicts = probe.stdout.strip().splitlines()[-3:]
    assert module_path == str(repo / 'lib' / '__init__.py')
    assert lists == "['marginlab.ai'] ['junk.example']"
    assert verdicts == 'True True block'
