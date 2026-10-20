#!/usr/bin/env python3
"""tests/providers/test_provider_face_per_wire.py — one Offering, faces split by wire id.

WHY
===
A gateway can expose a deployment only through ONE face and only when a
selector header names its serving provider. Measured on your-llm-gateway.example.com
(App **0733, 2026-10-20), for ``gpt-6.1-sol``:

    POST /v1/openai/native  model=gpt-6.1-sol                     -> RPM-walled
    POST /v1/responses      model=gpt-6.1-sol                     -> RPM-walled
    POST /v1/responses      model=gpt-6.1-sol + x-context-provider -> 400
                                    该模型在 provider 下无可用账号
    POST /v1/openai/native  model=kunlunbedrock.gpt-6.1-sol-m17   -> 400 配置不存在
    POST /v1/responses      model=kunlunbedrock.gpt-6.1-sol-m17   -> 200 invalid_prompt 配置不存在
    POST /v1/responses      model=kunlunbedrock.gpt-6.1-sol-m17
                            + x-context-provider: kunlun          -> 200, streams

So the selector is per-DEPLOYMENT: the face carrying it must not be reachable
from any wire id that lacks an account under that selector, and a wire id that
needs it must not fall back to the default face (which 404s ``配置不存在``).

WHAT IS PINNED
--------------
  * ``resolve_face(..., wire_id=...)`` honours ``request_faces`` per wire id.
  * An undeclared face name is refused, never silently defaulted.
  * ``FaceResolution.extra_headers`` carries the selector of the CHOSEN face
    only — a default-face model must not receive it.
  * ``build_probe_work`` probes each wire id on its own face and hands the
    selector to that cell, so the matrix cannot report a false ``not_found``.
  * Legacy migration compiles each wire id onto its own Connection.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/providers/test_provider_face_per_wire.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

CHAT_URL = 'https://gw.example.com/v1/openai/native'
KUNLUN_URL = 'https://gw.example.com/v1'
SELECTOR = {'x-context-provider': 'kunlun'}

PROVIDER = {
    'id': 'gw',
    'base_url': CHAT_URL,
    'protocol': 'openai',
    'faces': {
        'kunlun': {
            'base_url': KUNLUN_URL,
            'protocol': 'responses',
            'responses_profile': 'compatible',
            'extra_headers': SELECTOR,
        },
    },
}

SPLIT_MODEL = {
    'model_id': 'gpt-6.1-sol',
    'capabilities': ['text'],
    'request_ids': ['gpt-6.1-sol', 'kunlunbedrock.gpt-6.1-sol-m17'],
    'request_faces': {'kunlunbedrock.gpt-6.1-sol-m17': 'kunlun'},
}


def _resolve(model, wire_id=''):
    from lib.llm_dispatch.provider_face import resolve_face
    return resolve_face(PROVIDER, model, wire_id=wire_id)


def test_each_wire_id_resolves_to_its_own_face():
    public = _resolve(SPLIT_MODEL, wire_id='gpt-6.1-sol')
    assert public.ok, public.error
    assert public.base_url == CHAT_URL
    assert public.protocol in ('', 'openai')
    assert public.extra_headers == {}, (
        'the default face must not inherit another face selector: %r'
        % (public.extra_headers,))

    alias = _resolve(SPLIT_MODEL, wire_id='kunlunbedrock.gpt-6.1-sol-m17')
    assert alias.ok, alias.error
    assert alias.face_name == 'kunlun'
    assert alias.base_url == KUNLUN_URL
    assert alias.protocol == 'responses'
    assert alias.extra_headers == SELECTOR


def test_without_a_wire_id_the_entry_stays_on_the_default_face():
    """Backward compatibility: a caller that does not know the wire id (the
    Settings card, the matrix row) must still resolve the logical model."""
    r = _resolve(SPLIT_MODEL)
    assert r.ok, r.error
    assert r.face_name == 'default'
    assert r.base_url == CHAT_URL


def test_an_undeclared_request_face_is_refused_not_defaulted():
    bad = dict(SPLIT_MODEL,
               request_faces={'kunlunbedrock.gpt-6.1-sol-m17': 'kunlunn'})
    r = _resolve(bad, wire_id='kunlunbedrock.gpt-6.1-sol-m17')
    assert not r.ok
    assert 'kunlunn' in (r.error or '')


def test_the_shipped_recipe_governs_every_alias_it_lists():
    """Measured 400 该模型在 provider 下无可用账号 — pin it in the data too.

    A wire id that is not itself a face name in ``request_faces`` rides the
    default face. That is only safe while the template lists a selector for
    every id that needs one.
    """
    import json

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    path = os.path.join(root, 'bundles', 'yourprovider', 'provider_templates',
                        'yourprovider.json')
    with open(path, encoding='utf-8') as fh:
        tpl = json.load(fh)
    recipe = next(r for r in tpl['offering_recipes']
                  if r['model_id'] == 'gpt-6.1-sol')
    request_faces = recipe.get('request_faces') or {}
    assert 'gpt-6.1-sol' not in request_faces
    for wire_id, face_name in request_faces.items():
        assert wire_id in (recipe.get('request_ids') or []), wire_id
        assert face_name in tpl['faces'], (wire_id, face_name)

    from lib.provider.provider_template_recipes import (
        compile_provider_template_bundle)
    bundle = compile_provider_template_bundle(
        'yourprovider', selected_model_ids=['gpt-6.1-sol'])
    connections = {c['connection_id']: c for c in bundle['connections']}
    by_wire = {d['wire_model_id']: connections[d['connection_id']]
               for d in bundle['deployments']}
    alias_conn = by_wire['kunlunbedrock.gpt-6.1-sol-m17']
    assert alias_conn['protocol'] == 'openai_responses'
    assert alias_conn['extra_headers'] == {'x-context-provider': 'kunlun'}
    assert (alias_conn['inference_profile']['sampling']['temperature']
            == 'omit'), alias_conn.get('inference_profile')
    public_conn = by_wire['gpt-6.1-sol']
    assert public_conn['extra_headers'] == {}, public_conn['extra_headers']


def test_probe_work_probes_each_wire_on_its_own_face():
    from lib.provider.provider_probe import build_probe_work

    work = build_probe_work(PROVIDER, [SPLIT_MODEL], ['k1'])
    by_wire = {item[3]: item for item in work}
    assert set(by_wire) == {'gpt-6.1-sol', 'kunlunbedrock.gpt-6.1-sol-m17'}

    public = by_wire['gpt-6.1-sol']
    assert public[5] == CHAT_URL
    assert public[6] in ('', 'openai')
    assert len(public) < 8 or not (public[7].get('extra_headers') or {}), (
        'the selector leaked onto the public wire: %r' % (public[7],))

    alias = by_wire['kunlunbedrock.gpt-6.1-sol-m17']
    assert alias[5] == KUNLUN_URL
    assert alias[6] == 'responses'
    assert alias[7]['extra_headers'] == SELECTOR


def test_probe_worker_sends_the_selector_only_to_the_pinned_cell():
    import lib.provider.provider_probe as pp

    from lib.provider.provider_probe import build_probe_work

    seen = []

    def _fake_probe(base_url, api_key, model_id, extra_headers, timeout,
                    protocol='openai', oauth=''):
        seen.append({'model_id': model_id, 'protocol': protocol,
                     'headers': dict(extra_headers or {})})
        return 'ok', 'HTTP 200'

    task = {
        'provider_id': 'gw', 'status': 'running', 'started_at': 0,
        'finished_at': None, 'total': 2, 'done_count': 0, 'cells': {},
        'summary': {'ok': 0, 'disable': 0}, 'error': None, 'attempts': 1,
        '_abort': False, '_base_url': CHAT_URL,
        '_extra_headers': {}, '_protocol': 'openai', '_oauth': '',
    }
    work = build_probe_work(PROVIDER, [SPLIT_MODEL], ['k1'])

    orig, orig_persist = pp.probe_one_cell, pp.persist_probe_task
    pp.probe_one_cell, pp.persist_probe_task = _fake_probe, lambda t: None
    try:
        pp.run_cell_probe_task(task, work, timeout=5)
    finally:
        pp.probe_one_cell, pp.persist_probe_task = orig, orig_persist

    by_wire = {row['model_id']: row for row in seen}
    assert by_wire['kunlunbedrock.gpt-6.1-sol-m17']['headers'] == SELECTOR
    assert 'x-context-provider' not in by_wire['gpt-6.1-sol']['headers']


def _plan(legacy: dict):
    from lib.model_routing.migration import plan_legacy_migration
    return plan_legacy_migration({'providers': [legacy]})


def test_legacy_migration_compiles_each_wire_onto_its_own_connection():
    plan = _plan({
        'name': 'gw',
        'base_url': CHAT_URL,
        'protocol': 'openai',
        'faces': {name: dict(spec) for name, spec in
                  PROVIDER['faces'].items()},
        'api_keys': ['k1'],
        'models': [dict(SPLIT_MODEL)],
    })
    assert not plan.blocking_issues, [i.message for i in plan.blocking_issues]
    document = plan.document

    connections = {c['connection_id']: c for c in document['connections']}
    kunlun = [c for c in connections.values()
              if c.get('gateway_namespace') == 'kunlun']
    assert len(kunlun) == 1, connections
    assert kunlun[0]['extra_headers'] == SELECTOR
    # ``openai_responses`` is the stored spelling the v2 contract enumerates;
    # ``lib/llm_dispatch/slot.py`` normalizes it to ``responses`` at the shared
    # Slot boundary, so transport still sees the wire name.
    assert kunlun[0]['protocol'] == 'openai_responses'
    assert kunlun[0]['responses_profile'] == 'compatible'

    by_wire = {d['wire_model_id']: d for d in document['deployments']}
    assert by_wire['kunlunbedrock.gpt-6.1-sol-m17']['connection_id'] == \
        kunlun[0]['connection_id']
    assert by_wire['gpt-6.1-sol']['connection_id'] != kunlun[0]['connection_id']
    default_conn = connections[by_wire['gpt-6.1-sol']['connection_id']]
    assert default_conn['extra_headers'] == {}, default_conn


def test_migration_reports_an_undeclared_request_face():
    plan = _plan({
        'name': 'gw', 'base_url': CHAT_URL, 'protocol': 'openai',
        'api_keys': ['k1'],
        'models': [dict(SPLIT_MODEL)],
    })
    assert any(i.code == 'unknown_request_face' for i in plan.issues), (
        [i.code for i in plan.issues])


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
