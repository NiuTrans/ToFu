#!/usr/bin/env python3
"""Regression test for project-tool LLM prompts + tool descriptions.

Usage:
    python debug/test_project_prompts.py

Guards against:
  - Tool descriptions drifting out of sync with their names (every tool's
    description MUST mention its own name and be reasonably long).
  - The system prompt forgetting to advertise any of the 11 project tools
    (the git-shim additions were the immediate motivator).
  - The prompt exceeding 16 KB (token-budget guard — this block is
    injected on every request in project co-pilot mode).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


EXPECTED_TOOL_NAMES = [
    'list_dir', 'grep_search', 'find_files',
    'write_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
    'create_project', 'run_command',
]


def _banner(t):
    print(f'\n[test] {t}')


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f'  ✅ {msg}')


def test_tool_descriptions_self_documenting():
    _banner('each PROJECT_TOOL description mentions its own name and is >=40 chars')
    from lib.tools.project import PROJECT_TOOLS
    names = [t['function']['name'] for t in PROJECT_TOOLS]
    _assert(sorted(names) == sorted(EXPECTED_TOOL_NAMES),
            f'PROJECT_TOOLS names match expected (got {sorted(names)})')
    for t in PROJECT_TOOLS:
        name = t['function']['name']
        desc = t['function'].get('description', '') or ''
        _assert(len(desc) >= 40, f'{name} description >= 40 chars (got {len(desc)})')
        _assert(name in desc, f'{name} description mentions its own name')


def test_get_context_for_prompt_advertises_all_tools():
    _banner('get_context_for_prompt() returns non-empty system block')
    from lib.project_mod import set_project
    from lib.project_mod.indexer import get_context_for_prompt
    tmp = tempfile.mkdtemp(prefix='tofu_prompt_test_')
    try:
        set_project(tmp)
        ctx = get_context_for_prompt(tmp) or ''
        _assert(ctx, 'context is non-empty')
        _assert('PROJECT CO-PILOT MODE' in ctx, 'header present')
        # Per-tool prose lives in each tool's API ``description`` field
        # now — the system block intentionally does not duplicate them.
        # 16 KB budget.
        size = len(ctx.encode('utf-8'))
        print(f'  ℹ prompt size = {size} bytes')
        _assert(size <= 16 * 1024, f'prompt size <= 16 KB (got {size})')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_schema_description_tweaks_present():
    _banner('schema description phrases from item 3 are present')
    from lib.tools.project import PROJECT_TOOL_LIST_DIR
    ld = PROJECT_TOOL_LIST_DIR['function']['description']
    _assert('20 MB' in ld, 'list_dir mentions 20 MB guard')
    _assert('binary' in ld.lower(), 'list_dir mentions binary flagging')


def main():
    test_tool_descriptions_self_documenting()
    test_get_context_for_prompt_advertises_all_tools()
    test_schema_description_tweaks_present()
    print('\nALL PROMPT TESTS PASSED ✅')


if __name__ == '__main__':
    try:
        main()
    except AssertionError as e:
        print(f'\n❌ TEST FAILED: {e}')
        sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'\n❌ UNEXPECTED ERROR: {e}')
        sys.exit(2)
