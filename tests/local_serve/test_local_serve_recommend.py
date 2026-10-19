#!/usr/bin/env python3
"""tests/local_serve/test_local_serve_recommend.py — recommend/diagnose surface.

Pins the pieces the redesigned 本地部署 page and the chat agent share:

- ``_catalog.evaluate_hardware`` ranks the curated catalog against a
  hardware snapshot (comfortable-first, then bigger params), honors the
  purpose filter with an unfiltered fallback, and attaches download hints;
- ``_catalog.cloud_alternative`` appears exactly when local hardware can't
  comfortably run a capable model, and points at deepseek-v4-flash with
  real pricing + the DeepSeek provider-template key the page deep-links;
- ``_diagnose.diagnose_failure`` classifies known failure kinds with the
  right retryable flags and never raises on garbage;
- ``api.recommend`` shape and the ``local_serve_recommend`` chat handler
  formatting (hardware line, ranked candidates, cloud escape hatch);
- the read-only routes stay three GETs under the providers scope.

Run isolated (project convention): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

pytestmark = pytest.mark.unit

GiB = 1 << 30


def _hw(gpu_vram_gib=None, ram_gib=64, disk_gib=500):
    gpus = []
    if gpu_vram_gib:
        gpus = [{'index': 0, 'name': 'TestGPU',
                 'vram_free_bytes': gpu_vram_gib * GiB,
                 'vram_total_bytes': gpu_vram_gib * GiB}]
    return {'gpus': gpus,
            'ram_available_bytes': ram_gib * GiB,
            'ram_total_bytes': ram_gib * GiB,
            'disk_free_bytes': disk_gib * GiB}


# ─────────────────────────── catalog ranking ───────────────────────────

class TestEvaluateHardware:
    def test_strong_gpu_ranks_comfortable_first_then_params(self):
        from lib.local_serve._catalog import evaluate_hardware
        cands = evaluate_hardware(_hw(gpu_vram_gib=24))
        assert cands, 'a 24 GiB GPU must run something'
        tiers = [c['tier'] for c in cands]
        rank = {'comfortable': 0, 'tight': 1, 'extreme': 2, 'cpu': 3}
        assert [rank[t] for t in tiers] == sorted(rank[t] for t in tiers)
        comfortable = [c for c in cands if c['tier'] == 'comfortable']
        assert comfortable[0]['params_b'] >= comfortable[-1]['params_b']
        for cand in cands:
            best = cand['best']
            assert best['engine'] and best['weight_bytes'] > 0
            assert best['download']['hf_cmd'].startswith(
                'huggingface-cli download ')

    def test_purpose_filter_narrows_to_tagged_models(self):
        from lib.local_serve._catalog import (LOCAL_MODEL_CATALOG,
                                              evaluate_hardware)
        cands = evaluate_hardware(_hw(gpu_vram_gib=48), purpose='coding')
        assert cands
        assert all('coding' in c['tags'] for c in cands)
        # The catalog actually carries coding models, so this is a real
        # filter, not the fallback.
        assert any('coding' in e['tags'] for e in LOCAL_MODEL_CATALOG)

    def test_unknown_purpose_falls_back_to_unfiltered(self):
        from lib.local_serve._catalog import evaluate_hardware
        hw = _hw(gpu_vram_gib=24)
        assert (evaluate_hardware(hw, purpose='nonsense')
                == evaluate_hardware(hw))

    def test_no_gpu_tiny_ram_yields_nothing(self):
        from lib.local_serve._catalog import evaluate_hardware
        # Even the smallest GGUF (1.7B Q4 ≈ 1.1 GiB + context) needs headroom.
        assert evaluate_hardware(_hw(ram_gib=1)) == []

    def test_no_gpu_big_ram_falls_to_cpu_tier(self):
        from lib.local_serve._catalog import evaluate_hardware
        cands = evaluate_hardware(_hw(ram_gib=64))
        assert cands and all(c['tier'] == 'cpu' for c in cands)


# ─────────────────────────── cloud escape hatch ───────────────────────────

class TestCloudAlternative:
    def test_absent_when_local_is_comfortably_capable(self):
        from lib.local_serve._catalog import (cloud_alternative,
                                              evaluate_hardware)
        hw = _hw(gpu_vram_gib=24)
        assert cloud_alternative(hw, evaluate_hardware(hw)) is None

    def test_present_when_nothing_fits(self):
        from lib.local_serve._catalog import cloud_alternative
        alt = cloud_alternative(_hw(ram_gib=8), [])
        assert alt['kind'] == 'cloud_api'
        assert alt['model_id'] == 'deepseek-flash'  # V4.1 Flash (2026-09-10)
        # The settings page deep-links this key into the template wizard.
        assert alt['template_key'] == 'deepseek'
        assert alt['reason']
        pricing = alt['pricing']
        assert pricing['input'] and pricing['output']

    def test_present_when_only_tiny_models_fit(self):
        from lib.local_serve._catalog import (cloud_alternative,
                                              evaluate_hardware)
        hw = _hw(ram_gib=64)  # CPU-only: fits, but nothing ≥7B comfortable.
        alt = cloud_alternative(hw, evaluate_hardware(hw))
        assert alt is not None and alt['model_id'] == 'deepseek-flash'


# ─────────────────────────── failure diagnosis ───────────────────────────

class TestDiagnose:
    @pytest.mark.parametrize('err,tail,kind,retryable', [
        ('', 'torch.cuda.OutOfMemoryError: CUDA out of memory',
         'oom_exhausted', True),
        ('', 'OSError: libcuda.so.1: cannot open shared object file',
         'cuda_driver', False),
        ('', 'ValueError: The model architecture qwen99 is not supported',
         'arch_unsupported', False),
        ('No space left on device', '', 'disk_full', True),
        ('', 'OSError: Address already in use', 'port_conflict', True),
        ('创建隔离环境失败：Connection timed out', '', 'network', True),
        ('等待就绪超时', '', 'start_timeout', True),
        ('Tofu 服务重启导致部署中断', '', 'interrupted', True),
        ('something never seen before', 'garbage log', 'unknown', False),
    ])
    def test_classification(self, err, tail, kind, retryable):
        from lib.local_serve._diagnose import diagnose_failure
        diag = diagnose_failure(
            {'last_error': err, 'engine': 'vllm'}, tail)
        assert diag['kind'] == kind
        assert diag['retryable'] is retryable
        assert diag['summary'] and diag['suggestions']

    def test_never_raises_on_garbage(self):
        from lib.local_serve._diagnose import diagnose_failure
        assert diagnose_failure(None, None)['kind'] == 'unknown'


# ─────────────────────────── api.recommend + chat handler ───────────────────────────

class TestRecommendApi:
    def test_shape(self, monkeypatch):
        from lib.local_serve import api
        monkeypatch.setattr('lib.local_serve._probe.probe_hardware',
                            lambda: _hw(gpu_vram_gib=24))
        result = api.recommend('coding')
        assert result['ok'] is True
        assert result['purpose'] == 'coding'
        assert result['hardware']['gpus'][0]['name'] == 'TestGPU'
        assert result['candidates']
        assert 'cloud_alternative' in result


def _invoke(fn_name, fn_args):
    from lib.tasks_pkg.handlers.local_serve import _handle_local_serve_tool
    task = {'id': 'task_rec_test', 'aborted': False, '_userId': 1,
            '_tool_approval_receipts': {}}
    round_entry = {'toolCallId': 'tc_1', 'status': 'running', 'query': ''}
    return _handle_local_serve_tool(
        task, None, fn_name, 'tc_1', fn_args, 1, round_entry,
        {}, None, False)


class TestRecommendHandler:
    def test_registry_includes_recommend(self):
        from lib.local_serve.tool_defs import LOCAL_SERVE_TOOL_NAMES
        assert 'local_serve_recommend' in LOCAL_SERVE_TOOL_NAMES

    def test_formats_candidates_and_cloud_escape(self, monkeypatch):
        monkeypatch.setattr(
            'lib.local_serve.api.recommend',
            lambda purpose=None: {
                'ok': True, 'purpose': purpose or '',
                'hardware': _hw(gpu_vram_gib=24),
                'candidates': [{
                    'id': 'qwen3-8b', 'display': 'Qwen3 8B', 'params_b': 8,
                    'tier': 'comfortable', 'note': '',
                    'best': {'variant': 'BF16', 'engine': 'vllm',
                             'weight_bytes': 16 * GiB,
                             'download': {
                                 'hf_cmd': 'huggingface-cli download Qwen/x',
                                 'modelscope_cmd': 'modelscope download --model Qwen/x'}},
                    'alternatives': []}],
                'cloud_alternative': {
                    'kind': 'cloud_api', 'template_key': 'deepseek',
                    'model_id': 'deepseek-v4-flash',
                    'display': 'DeepSeek V4 Flash', 'reason': '测试原因',
                    'pricing': {'input': 0.14, 'output': 0.28}}})
        _tc, content, _ = _invoke('local_serve_recommend', {'purpose': 'chat'})
        assert '硬件' in content and '推荐' in content
        assert 'Qwen3 8B' in content and '轻松运行' in content
        assert 'huggingface-cli download' in content
        assert '国内镜像' in content
        assert '云端替代' in content and 'DeepSeek V4 Flash' in content
        assert 'local_serve_prepare' in content

    def test_empty_catalog_still_guides(self, monkeypatch):
        monkeypatch.setattr(
            'lib.local_serve.api.recommend',
            lambda purpose=None: {
                'ok': True, 'purpose': '', 'hardware': _hw(ram_gib=8),
                'candidates': [], 'cloud_alternative': None})
        _tc, content, _ = _invoke('local_serve_recommend', {})
        assert '没有模型' in content

    def test_failed_status_surfaces_diagnosis(self, monkeypatch):
        monkeypatch.setattr(
            'lib.local_serve.api.deployment_status',
            lambda iid, **_kwargs: {
                'ok': True, 'id': iid, 'status': 'failed', 'engine': 'vllm',
                'model_path': '/m', 'last_error': 'boom',
                'diagnosis': {'kind': 'oom_exhausted',
                              'summary': '显存不足，且自动降级链已用尽',
                              'suggestions': ['关闭其它占用显存的程序'],
                              'retryable': True},
                'log_tail': 'trace...OOM'})
        _tc, content, _ = _invoke('local_serve_status',
                                  {'instance_id': 'ls_x'})
        assert '诊断（oom_exhausted）' in content
        assert '显存不足' in content
        assert '关闭其它占用显存的程序' in content
        assert '重试部署是否有意义：是' in content


# ─────────────────────────── read-only routes ───────────────────────────

class TestRoutes:
    def test_blueprint_exposes_three_read_only_gets(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'routes', 'api_v1', 'local_serve.py')
        with open(path, encoding='utf-8') as f:
            src = f.read()
        # Source guard: v1 route modules declare canonical /api/v1 handler
        # paths; the v4 surface is the projected twin
        # (lib/api_v4.copy_blueprint_to_v4).
        assert '"/api/v1/local-serve/hardware"' in src
        assert '"/api/v1/local-serve/recommendations"' in src
        assert '"/api/v1/local-serve/deployments"' in src
        # Read-only surface: writes stay with the agent tool family.
        assert 'methods=["GET"]' in src
        assert 'methods=["POST"]' not in src
        assert src.count('@require_scope("providers")') == 3

    def test_blueprint_registered(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'routes', 'api_v1', '__init__.py')
        with open(path, encoding='utf-8') as f:
            src = f.read()
        assert 'api_v1_local_serve_bp' in src
