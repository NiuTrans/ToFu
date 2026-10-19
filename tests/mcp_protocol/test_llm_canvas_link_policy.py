"""Shared canvas policy guard; no network or MCP process required."""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from lib.tasks_pkg.tool_display._mcp import _mcp_links

pytestmark = pytest.mark.unit
POLICY = json.loads((Path(__file__).resolve().parents[2] / 'contracts' /
                     'llm_canvas_links_v1.json').read_text(encoding='utf-8'))


def test_policy_is_bounded_verified_https_resource_contract():
    origin = urlsplit(POLICY['origin'])
    assert origin.scheme == 'https'
    assert origin.netloc == 'model.internal.example.com'
    assert not origin.path and not origin.query and not origin.fragment
    assert POLICY['version'] == 1
    assert POLICY['provenance']['pathRoute']
    assert POLICY['limits'] == dict(serializedChars=65536, referenceChars=512,
                                    labelChars=40, inputLinks=2)


def test_backend_emission_and_server_scope_follow_contract():
    tool = f"mcp__{POLICY['server']}__{POLICY['toolPrefix']}get"
    url = POLICY['origin'] + POLICY['canvasPathPrefix'] + '381153'
    for key in POLICY['inputKeys']:
        assert _mcp_links({key: '381153'}, fn_name=tool) == {'381153': url}
    assert not _mcp_links({'experiment_id': '381153'},
                          fn_name='mcp__other__experiment_get')


def test_backend_run_url_is_projected_to_canvas_not_task():
    base = POLICY['origin'] + POLICY['canvasPathPrefix']
    assert _mcp_links({'experiment_or_url': base + '381153/991/0'},
                      fn_name='mcp__llm__experiment_get') == {'381153': base + '381153'}
