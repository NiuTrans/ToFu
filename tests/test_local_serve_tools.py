#!/usr/bin/env python3
"""tests/test_local_serve_tools.py — agent tool family wiring.

Pins the registry contract (searchable family, write/confirmation
partitions), the handler's approval-receipt boundaries (deploy/remove fail
closed without a receipt), the async deploy kick-off, and the approval
enrichers / display labels. All serving work is mocked at the api facade.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.local_serve.tool_defs import LOCAL_SERVE_TOOL_NAMES

pytestmark = pytest.mark.unit


# ─────────────────────────── registry assembly ───────────────────────────

def _ctx(**over):
    from lib.tools.registry._spec import ToolContext
    base = dict(
        cfg={}, task_id='task_ctx_test', project_path='',
        project_enabled=False, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False,
        browser_enabled=False, desktop_enabled=False,
        has_base_tools=True)
    base.update(over)
    return ToolContext(**base)


class TestRegistryBuild:
    def test_family_builds_with_base_tools(self):
        from lib.tools.registry._build import _build_local_serve
        tools = _build_local_serve(_ctx())
        names = {t['function']['name'] for t in tools}
        assert names == set(LOCAL_SERVE_TOOL_NAMES)

    def test_env_kill_switch(self, monkeypatch):
        from lib.tools.registry._build import _build_local_serve
        monkeypatch.setenv('TOFU_LOCAL_SERVE', '0')
        assert _build_local_serve(_ctx()) == []

    def test_no_base_tools_no_family(self):
        from lib.tools.registry._build import _build_local_serve
        assert _build_local_serve(_ctx(has_base_tools=False)) == []

    def test_spec_partitions(self):
        from lib.tools.registry import all_specs
        spec = next(s for s in all_specs() if s.key == 'local_serve')
        assert spec.discovery_policy == 'searchable'
        assert {'local_serve_deploy', 'local_serve_remove'} <= set(
            spec.confirmation_tools)
        assert {'local_serve_deploy', 'local_serve_stop',
                'local_serve_remove'} <= set(spec.write_tools)
        assert 'local_serve_prepare' not in set(spec.write_tools)


# ─────────────────────────── handler ───────────────────────────

def _task():
    return {'id': 'task_local_serve_test', 'aborted': False,
            '_userId': 1,
            '_tool_approval_receipts': {}}


def _invoke(fn_name, fn_args, task=None):
    from lib.tasks_pkg.handlers.local_serve import _handle_local_serve_tool
    task = task or _task()
    round_entry = {'toolCallId': 'tc_1', 'status': 'running', 'query': ''}
    return _handle_local_serve_tool(
        task, None, fn_name, 'tc_1', fn_args, 1, round_entry,
        {}, None, False)


@pytest.fixture
def approve(monkeypatch):
    """Force the approval receipt check to pass."""
    monkeypatch.setattr(
        'lib.tasks_pkg.tool_dispatch._approval.consume_approval_receipt',
        lambda task, name, tc_id, args: True)


class TestPrepareHandler:
    def test_formats_plan(self, monkeypatch):
        monkeypatch.setattr(
            'lib.local_serve.api.prepare',
            lambda path, engine=None: {
                'ok': True,
                'inspection': {'format': 'hf', 'architecture': 'qwen3',
                               'path': path},
                'hardware': {'gpus': [{'index': 0, 'name': 'RTX 4090',
                                       'vram_free_bytes': 20 << 30,
                                       'vram_total_bytes': 24 << 30}],
                             'ram_available_bytes': 64 << 30,
                             'disk_free_bytes': 500 << 30},
                'plan': {'engine': 'vllm', 'tier': 'comfortable',
                         'served_name': 'Qwen3-8B',
                         'base_url': 'http://127.0.0.1:18100/v1',
                         'argv': ['vllm', 'serve', '/m'],
                         'notes': ['n1'], 'degrade': [{'note': '降上下文'}]},
            })
        _tc, content, _ = _invoke('local_serve_prepare',
                                  {'model_path': '/m'})
        assert 'vllm' in content and 'RTX 4090' in content
        assert 'local_serve_deploy' in content

    def test_error_is_user_facing(self, monkeypatch):
        monkeypatch.setattr(
            'lib.local_serve.api.prepare',
            lambda path, engine=None: {'ok': False, 'stage': 'inspect',
                                       'error': '路径不存在'})
        _tc, content, _ = _invoke('local_serve_prepare',
                                  {'model_path': '/nope'})
        assert '路径不存在' in content


class TestDeployHandler:
    def test_receipt_required(self, monkeypatch):
        monkeypatch.setattr(
            'lib.tasks_pkg.tool_dispatch._approval.consume_approval_receipt',
            lambda task, name, tc_id, args: False)
        monkeypatch.setattr(
            'lib.local_serve.api.create_deployment',
            lambda *a, **k: pytest.fail('must not create without receipt'))
        tc, content, _ = _invoke('local_serve_deploy', {'model_path': '/m'})
        assert '批准' in content

    def test_async_kickoff(self, monkeypatch, approve):
        monkeypatch.setattr(
            'lib.local_serve.api.create_deployment',
            lambda path, **_kwargs: {
                'ok': True, 'instance': {'id': 'ls_vllm_m'}})
        seen = {}

        def _fake_async(iid, **_kwargs):
            seen['iid'] = iid
            return {'ok': True, 'started': True}

        monkeypatch.setattr(
            'lib.local_serve.api.start_deployment_async', _fake_async)
        _tc, content, _ = _invoke('local_serve_deploy', {'model_path': '/m'})
        assert seen['iid'] == 'ls_vllm_m'
        assert 'ls_vllm_m' in content
        assert 'local_serve_status' in content

    def test_create_failure_propagates(self, monkeypatch, approve):
        monkeypatch.setattr(
            'lib.local_serve.api.create_deployment',
            lambda path, **_kwargs: {'ok': False, 'stage': 'plan',
                                     'error': '端口段已满'})
        _tc, content, _ = _invoke('local_serve_deploy', {'model_path': '/m'})
        assert '端口段已满' in content


class TestRemoveHandler:
    def test_receipt_required(self, monkeypatch):
        monkeypatch.setattr(
            'lib.tasks_pkg.tool_dispatch._approval.consume_approval_receipt',
            lambda task, name, tc_id, args: False)
        _tc, content, _ = _invoke('local_serve_remove',
                                  {'instance_id': 'ls_x'})
        assert '批准' in content

    def test_remove_ok(self, monkeypatch, approve):
        monkeypatch.setattr('lib.local_serve.api.remove_deployment',
                            lambda iid, **_kwargs: {'ok': True})
        _tc, content, _ = _invoke('local_serve_remove',
                                  {'instance_id': 'ls_x'})
        assert '已移除' in content


class TestReadHandlers:
    def test_list_empty(self, monkeypatch):
        monkeypatch.setattr('lib.local_serve.api.list_deployments',
                            lambda **_kwargs: {'ok': True, 'instances': []})
        _tc, content, _ = _invoke('local_serve_list', {})
        assert '没有' in content

    def test_status_failed_shows_log(self, monkeypatch):
        monkeypatch.setattr(
            'lib.local_serve.api.deployment_status',
            lambda iid, **_kwargs: {
                'ok': True, 'id': iid, 'status': 'failed',
                'engine': 'vllm', 'model_path': '/m',
                'last_error': 'OOM', 'log_tail': 'trace...OOM'})
        _tc, content, _ = _invoke('local_serve_status',
                                  {'instance_id': 'ls_x'})
        assert 'OOM' in content and '日志尾部' in content


# ─────────────────────────── approval enrichers ───────────────────────────

class TestApprovalEnrichers:
    def test_deploy_risk_fields(self):
        from lib.tasks_pkg.tool_dispatch._approval import (
            _APPROVAL_META_ENRICHERS,
        )
        meta = {'approvalId': 'a', 'toolName': 'local_serve_deploy',
                'path': '', 'description': ''}
        _APPROVAL_META_ENRICHERS['local_serve_deploy'](
            meta, {'model_path': '/models/Qwen3-8B', 'engine': 'vllm'})
        assert meta['path'] == '/models/Qwen3-8B'
        labels = [r['label'] for r in meta['riskFields']]
        assert 'Model path to deploy' in labels
        assert 'venv' in meta['description']

    def test_stop_and_remove_registered(self):
        from lib.tasks_pkg.tool_dispatch._approval import (
            _APPROVAL_META_ENRICHERS,
        )
        for name in ('local_serve_stop', 'local_serve_remove'):
            meta = {'approvalId': 'a', 'toolName': name,
                    'path': '', 'description': ''}
            _APPROVAL_META_ENRICHERS[name](meta, {'instance_id': 'ls_x'})
            assert meta['path'] == 'ls_x' and meta['riskFields']


# ─────────────────────────── display labels ───────────────────────────

class TestDisplay:
    @pytest.mark.parametrize('fn_name,args,want', [
        ('local_serve_prepare', {'model_path': '/models/Qwen3-8B'},
         'Inspect local model: Qwen3-8B'),
        ('local_serve_deploy',
         {'model_path': '/models/Qwen3-8B', 'engine': 'vllm'},
         'Deploy local model: Qwen3-8B · vllm'),
        ('local_serve_status', {'instance_id': 'ls_x'},
         'Check local deployment status'),
        ('local_serve_list', {}, 'List local deployments'),
        ('local_serve_stop', {'instance_id': 'ls_x'},
         'Stop local deployment'),
        ('local_serve_remove', {'instance_id': 'ls_x'},
         'Remove local deployment'),
    ])
    def test_labels(self, fn_name, args, want):
        from lib.tasks_pkg.tool_display._renderers import (
            _tool_display_local_serve,
        )
        display, _meta = _tool_display_local_serve(fn_name, args, 'tc', '')
        assert display == want

    def test_dispatch_table_covers_family(self):
        from lib.tasks_pkg.tool_display._dispatch import (
            _TOOL_DISPLAY_DISPATCH,
        )
        for name in LOCAL_SERVE_TOOL_NAMES:
            assert name in _TOOL_DISPLAY_DISPATCH
