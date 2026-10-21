#!/usr/bin/env python3
"""tests/frontend/test_request_name_faces.py — request name → serving face.

WHY
===
A gateway can serve one wire spelling only on a specific face of the gateway:
aigc answers 200 for ``kunlunbedrock.gpt-6.1-sol-m17`` on ``/v1`` (Responses)
with ``x-context-provider: kunlun``, and 400 ``配置不存在`` on the card's default
``/openai/native`` Connection.

The Settings matrix used to bind every newly added request name to "the first
enabled Connection". The row then existed — and could never answer — so the
operator re-added the same name repeatedly and concluded the alias was broken.
The fix is a per-request-name route plan; this suite pins that plan, not the
widget that consumes it.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/frontend/test_request_name_faces.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_GATEWAY = {
    'key': 'meituan',
    'name': 'AIGC',
    'base_url': 'https://aigc.example/v1/openai/native',
    'faces': {
        'kunlun': {
            'base_url': 'https://aigc.example/v1',
            'protocol': 'responses',
            'responses_profile': 'compatible',
            'extra_headers': {'x-context-provider': 'kunlun'},
            'inference_profile': {'sampling': {'temperature': 'omit'}},
        },
    },
    'offering_recipes': [
        {
            'model_id': 'gpt-6.1-sol',
            'request_ids': ['gpt-6.1-sol', 'kunlunbedrock.gpt-6.1-sol-m17'],
            'request_faces': {'kunlunbedrock.gpt-6.1-sol-m17': 'kunlun'},
        },
        {'model_id': 'plain-model'},
    ],
}


def _node() -> str:
    return shutil.which('node') or 'node'


def _run(script: str) -> dict:
    module = native_module_path(
        'provider-request-names.js',
        ROOT + '/frontend/src/features/settings/provider-request-names.ts',
    )
    program = (
        "require(%s);"
        "const owner = globalThis;"
        "const out = (() => { %s })();"
        "process.stdout.write(JSON.stringify(out));"
        % (json.dumps(module), script)
    )
    result = subprocess.run([_node(), '-e', program], capture_output=True,
                            text=True, timeout=120, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_pinned_request_name_resolves_its_face():
    out = _run("""
        return owner.planRequestNameFaces(
          ['kunlunbedrock.gpt-6.1-sol-m17'],
          ['https://aigc.example/v1/openai/native'],
          %s);
    """ % json.dumps([_GATEWAY]))
    plan = out['kunlunbedrock.gpt-6.1-sol-m17']
    assert plan['face'] == 'kunlun'
    assert plan['base_url'] == 'https://aigc.example/v1'
    # The server refuses the alias without this selector; the plan carries it.
    assert plan['extra_headers'] == {'x-context-provider': 'kunlun'}
    # Responses wire, not chat: the alias is not served on the native path.
    assert plan['protocol'] == 'openai_responses'
    assert plan['responses_profile'] == 'compatible'
    assert plan['inference_profile'] == {'sampling': {'temperature': 'omit'}}


def test_an_unpinned_name_keeps_the_card_default_face():
    out = _run("""
        return owner.planRequestNameFaces(
          ['gpt-6.1-sol'],
          ['https://aigc.example/v1/openai/native'],
          %s);
    """ % json.dumps([_GATEWAY]))
    plan = out['gpt-6.1-sol']
    assert 'face' not in plan
    assert plan['base_url'] == 'https://aigc.example/v1/openai/native'
    assert plan['extra_headers'] == {}


def test_a_card_that_hosts_only_the_default_url_still_plans_the_face():
    """Old cards predate the face; staging must create it, not skip the name."""
    out = _run("""
        return owner.planRequestNameFaces(
          ['kunlunbedrock.gpt-6.1-sol-m17'],
          ['https://aigc.example/v1/openai/native'],
          %s);
    """ % json.dumps([_GATEWAY]))
    assert out['kunlunbedrock.gpt-6.1-sol-m17']['face'] == 'kunlun'


def test_the_matrix_badge_counts_headers_the_connection_already_sends():
    """``kunlunbedrock.gpt-6.1-sol-m17`` carried its selector on the Connection.

    The badge tested only ``deployment.extra_headers``, so the row rendered
    header-less — reported as "we added the header and now it is gone" — even
    though every request carried it. The effective set is the Connection's
    headers overlaid by the request name's own.
    """
    module = native_module_path(
        'provider-request-names.js',
        ROOT + '/frontend/src/features/settings/provider-request-names.ts')
    program = (
        "require(%s);"
        "const owner = globalThis;"
        "const out = (() => { %s })();"
        "process.stdout.write(JSON.stringify(out));"
        % (json.dumps(module), """
        const doc = {
          connections: [
            { connection_id: 'kunlun', extra_headers: { 'x-context-provider': 'kunlun' } },
            { connection_id: 'plain' },
          ],
          deployments: [
            { deployment_id: 'a', connection_id: 'kunlun' },
            { deployment_id: 'b', connection_id: 'kunlun' },
            { deployment_id: 'c', connection_id: 'plain' },
          ],
        };
        return {
          inherited: owner.connectionHeadersFor(doc, 'kunlun'),
          none: owner.connectionHeadersFor(doc, 'plain'),
          missing: owner.connectionHeadersFor(doc, 'gone'),
          shared: owner.otherRequestNamesOnConnection(doc, doc.deployments[0]),
          alone: owner.otherRequestNamesOnConnection(doc, doc.deployments[2]),
        };
        """))
    result = subprocess.run([_node(), '-e', program], capture_output=True,
                            text=True, timeout=120, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out['inherited'] == {'x-context-provider': 'kunlun'}
    assert out['none'] == {} and out['missing'] == {}
    # Two request names ride the kunlun Connection; deleting its header would
    # silently unroute the sibling, so the panel must refuse that removal.
    assert out['shared'] == 1
    assert out['alone'] == 0

def test_a_foreign_provider_hosting_neither_url_gets_no_plan():
    out = _run("""
        return owner.planRequestNameFaces(
          ['kunlunbedrock.gpt-6.1-sol-m17'],
          ['https://other.example/v1'],
          %s);
    """ % json.dumps([_GATEWAY]))
    assert out == {}


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
