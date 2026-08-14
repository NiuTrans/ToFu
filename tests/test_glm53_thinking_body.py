"""GLM-5.2/5.3 thinking-contract adaptation (official docs, 2026-08-14).

官方契约(Z.AI Deep Thinking 指南 + GLM-5.3 模型卡;docs.infini-ai.com
GLM thinking 教程互证):
  - GLM-5.3 拒绝 thinking.type='disabled'(强制思考模型,原生端点报错)
  - GLM-5.3 reasoning_effort 仅接受 low/high/max;GLM-5.2 接受七档兼容值
    (none/minimal 跳过思考,low/medium 归并 high,xhigh 归并 max)
  - clear_thinking=false 保留回传的 reasoning_content(工具/Agent 场景
    官方推荐:提升表现 + 缓存命中);仅在历史确实携带时才有意义
"""

import pytest

from lib.llm.body import build_body
from lib.llm_dispatch.api import _readjust_thinking_params
from lib.model_info import (
    glm_line_version,
    glm_reasoning_effort,
    is_glm53,
)

pytestmark = pytest.mark.unit


def _plain():
    return [{'role': 'user', 'content': 'hi'}]


def _with_trace():
    return [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'a', 'reasoning_content': 'trace'},
        {'role': 'user', 'content': 'next'},
    ]


# ── 版本解析 ──────────────────────────────────────────────

def test_glm_line_version_parses_generations():
    assert glm_line_version('glm-5.3') == (5, 3)
    assert glm_line_version('GLM-5.3[1m]') == (5, 3)  # coding-plan 1M suffix
    assert glm_line_version('GLM-5.2-Air') == (5, 2)
    assert glm_line_version('glm-5') == (5, 0)
    assert glm_line_version('glm-4.7') == (4, 7)
    assert glm_line_version('glm-zero-preview') is None
    assert glm_line_version('gpt-5.3') is None
    assert glm_line_version('qwen3.6-plus') is None


def test_is_glm53_gate():
    assert is_glm53('glm-5.3')
    assert is_glm53('glm-5.3[1m]')
    assert not is_glm53('glm-5.2')
    assert not is_glm53('glm-5')
    assert not is_glm53('glm-4.7')
    assert not is_glm53('qwen3.6')


# ── effort 阶梯映射 ────────────────────────────────────────

def test_glm53_effort_ladder():
    m = 'glm-5.3'
    assert glm_reasoning_effort('off', True, m) == 'low'
    assert glm_reasoning_effort('low', True, m) == 'low'
    assert glm_reasoning_effort('medium', True, m) == 'high'
    assert glm_reasoning_effort('high', True, m) == 'high'
    assert glm_reasoning_effort('xhigh', True, m) == 'max'
    assert glm_reasoning_effort('max', True, m) == 'max'
    assert glm_reasoning_effort(None, True, m) == 'high'  # 'medium' default
    assert glm_reasoning_effort('weird', True, m) == 'high'
    # 5.3 cannot disable thinking — off degrades to the cheapest rung
    assert glm_reasoning_effort('max', False, m) == 'low'


def test_glm52_effort_passthrough():
    m = 'glm-5.2'
    assert glm_reasoning_effort('low', True, m) == 'low'
    assert glm_reasoning_effort('medium', True, m) == 'medium'
    assert glm_reasoning_effort('high', True, m) == 'high'
    assert glm_reasoning_effort('xhigh', True, m) == 'xhigh'
    assert glm_reasoning_effort('max', True, m) == 'max'
    assert glm_reasoning_effort(None, True, m) == 'medium'
    # ≤5.2 disables via thinking.type; 'none' is the skip-thinking rung
    assert glm_reasoning_effort('max', False, m) == 'none'


# ── build_body:GLM-5.3 强制思考 ────────────────────────────

def test_glm53_never_sends_disabled():
    body = build_body('glm-5.3', _plain(), thinking_enabled=False)
    assert body['thinking']['type'] == 'enabled'
    assert 'clear_thinking' not in body['thinking']
    assert body['reasoning_effort'] == 'low'
    assert body['temperature'] == 1.0


@pytest.mark.parametrize('depth,expected', [
    ('off', 'low'),
    ('low', 'low'),
    ('medium', 'high'),
    ('high', 'high'),
    ('xhigh', 'max'),
    ('max', 'max'),
])
def test_glm53_depth_mapping(depth, expected):
    body = build_body('glm-5.3', _plain(), thinking_enabled=True,
                      thinking_depth=depth)
    assert body['thinking']['type'] == 'enabled'
    assert body['reasoning_effort'] == expected


def test_glm53_depth_off_degrades_to_low():
    body = build_body('glm-5.3', _plain(), thinking_enabled=True,
                      thinking_depth='off')
    assert body['thinking']['type'] == 'enabled'
    assert body['reasoning_effort'] == 'low'


# ── build_body:GLM-5.2 effort + preserved thinking ─────────

def test_glm52_off_wire_unchanged():
    body = build_body('glm-5.2', _plain(), thinking_enabled=False)
    assert body['thinking'] == {'type': 'disabled'}
    assert 'reasoning_effort' not in body


def test_glm52_effort_passthrough_on_wire():
    body = build_body('glm-5.2', _plain(), thinking_enabled=True,
                      thinking_depth='xhigh')
    assert body['thinking']['type'] == 'enabled'
    assert body['reasoning_effort'] == 'xhigh'


def test_glm52_clear_thinking_requires_history_trace():
    body = build_body('glm-5.2', _with_trace(), thinking_enabled=True)
    assert body['thinking'].get('clear_thinking') is False
    body = build_body('glm-5.2', _plain(), thinking_enabled=True)
    assert 'clear_thinking' not in body['thinking']


def test_glm53_clear_thinking_requires_history_trace():
    body = build_body('glm-5.3', _with_trace(), thinking_enabled=True)
    assert body['thinking'].get('clear_thinking') is False
    assert body['reasoning_effort'] == 'high'  # default depth → high
    body = build_body('glm-5.3', _plain(), thinking_enabled=True)
    assert 'clear_thinking' not in body['thinking']


# ── 旧 GLM 线(≤5.1 / 4.x)wire 完全不变 ────────────────────

@pytest.mark.parametrize('model', ['glm-5.1', 'glm-5', 'glm-4.7'])
def test_older_glm_wire_unchanged(model):
    body = build_body(model, _with_trace(), thinking_enabled=True)
    assert body['thinking'] == {'type': 'enabled'}
    assert 'reasoning_effort' not in body
    body = build_body(model, _plain(), thinking_enabled=False)
    assert body['thinking'] == {'type': 'disabled'}
    assert 'reasoning_effort' not in body


# ── 换模路径 _readjust_thinking_params 与 build_body lockstep ─

def test_readjust_glm53_forced_when_body_says_disabled():
    # GLM-5.2 off-body swapped onto a 5.3 slot: disabled is an API error
    # there — the swap path must degrade to enabled + low, not forward it.
    body = {'model': 'glm-5.2', 'messages': _plain(),
            'thinking': {'type': 'disabled'}}
    _readjust_thinking_params(body, 'glm-5.3', '')
    assert body['thinking']['type'] == 'enabled'
    assert body['reasoning_effort'] == 'low'
    assert body['temperature'] == 1.0


def test_readjust_carries_effort_across_glm_swap():
    # GLM→GLM slot swap (e.g. 5.2 slot 503 → 5.3 slot): the rung rides in
    # top-level reasoning_effort, which the old elif-ladder never reached
    # when thinking_dict declared the state — it silently reset to default.
    body = {'model': 'glm-5.2', 'messages': _plain(),
            'thinking': {'type': 'enabled'}, 'reasoning_effort': 'max'}
    _readjust_thinking_params(body, 'glm-5.3', '')
    assert body['reasoning_effort'] == 'max'


def test_readjust_glm53_clear_thinking_rebuilt():
    body = {'model': 'glm-5.2', 'messages': _with_trace(),
            'thinking': {'type': 'enabled', 'clear_thinking': False},
            'reasoning_effort': 'max'}
    _readjust_thinking_params(body, 'glm-5.3', '')
    assert body['thinking'].get('clear_thinking') is False
    body = {'model': 'glm-5.2', 'messages': _plain(),
            'thinking': {'type': 'enabled'}, 'reasoning_effort': 'max'}
    _readjust_thinking_params(body, 'glm-5.3', '')
    assert 'clear_thinking' not in body['thinking']


def test_readjust_older_glm_stays_legacy():
    body = {'model': 'glm-5.2', 'messages': _plain(),
            'thinking': {'type': 'enabled'}, 'reasoning_effort': 'max'}
    _readjust_thinking_params(body, 'glm-5.1', '')
    assert body['thinking'] == {'type': 'enabled'}
    assert 'reasoning_effort' not in body


# ── 供应商级 enable_thinking 不得抢占 GLM-5.2+ 原生契约 ────
# (live 证据 2026-08-14 your-llm-gateway.example.com:enable_thinking 对 glm-5.3 是死
# 字段 — 关不掉思考、effort 旋钮与 clear_thinking 全丢;yourprovider 模板把
# thinking_format=enable_thinking 配在供应商级,新装用户全部中招)

def test_provider_enable_thinking_does_not_shadow_glm53():
    body = build_body('glm-5.3', _plain(), thinking_enabled=True,
                      thinking_format='enable_thinking')
    assert body['thinking']['type'] == 'enabled'
    assert body['reasoning_effort'] == 'high'
    assert 'enable_thinking' not in body
    assert body['temperature'] == 1.0


def test_provider_enable_thinking_off_still_degrades_glm53():
    body = build_body('glm-5.3', _plain(), thinking_enabled=False,
                      thinking_format='enable_thinking')
    assert body['thinking']['type'] == 'enabled'
    assert body['reasoning_effort'] == 'low'
    assert 'enable_thinking' not in body


def test_provider_enable_thinking_glm52_real_disable():
    # 旧分支发 enable_thinking:false — GLM 网关的死字段,模型照思考;
    # 归位 GLM 分支后才是真关闭(thinking.type=disabled)。
    body = build_body('glm-5.2', _plain(), thinking_enabled=False,
                      thinking_format='enable_thinking')
    assert body['thinking'] == {'type': 'disabled'}
    assert 'enable_thinking' not in body


def test_provider_enable_thinking_glm52_clear_thinking():
    body = build_body('glm-5.2', _with_trace(), thinking_enabled=True,
                      thinking_format='enable_thinking')
    assert body['thinking'].get('clear_thinking') is False


def test_provider_enable_thinking_keeps_older_glm_qwen_wire():
    # ≤5.1 无 live 证据,保持供应商声明优先(wire 不变)
    body = build_body('glm-4.7', _plain(), thinking_enabled=True,
                      thinking_format='enable_thinking')
    assert body.get('enable_thinking') is True
    assert 'thinking' not in body


def test_engine_chat_template_kwargs_still_wins_on_glm53():
    # sglang/vLLM 自托管是引擎级声明,必须继续赢(文档明确 GLM 双模
    # 模型走 Jinja 模板闸门)
    body = build_body('glm-5.3', _plain(), thinking_enabled=True,
                      thinking_format='chat_template_kwargs')
    assert body['chat_template_kwargs'] == {'enable_thinking': True}
    assert 'thinking' not in body
    assert 'reasoning_effort' not in body


def test_engine_none_format_still_wins_on_glm53():
    body = build_body('glm-5.3', _plain(), thinking_enabled=True,
                      thinking_format='none')
    assert 'thinking' not in body
    assert 'reasoning_effort' not in body
    assert 'enable_thinking' not in body


def test_readjust_provider_enable_thinking_routes_to_glm_contract():
    body = {'model': 'glm-4.7', 'messages': _plain(),
            'enable_thinking': True, 'temperature': 0.7}
    _readjust_thinking_params(body, 'glm-5.3', 'enable_thinking')
    assert 'enable_thinking' not in body
    assert body['thinking']['type'] == 'enabled'
    assert body['reasoning_effort'] == 'high'


def test_readjust_engine_none_still_wins_on_glm53():
    body = {'model': 'glm-4.7', 'messages': _plain(),
            'enable_thinking': True}
    _readjust_thinking_params(body, 'glm-5.3', 'none')
    assert 'thinking' not in body
    assert 'enable_thinking' not in body
    assert 'reasoning_effort' not in body


def test_build_and_readjust_parity():
    msgs = _with_trace()
    fresh = build_body('glm-5.3', msgs, thinking_enabled=True,
                       thinking_depth='max')
    swapped = {'model': 'glm-5.2', 'messages': [dict(m) for m in msgs],
               'thinking': {'type': 'enabled', 'clear_thinking': False},
               'reasoning_effort': 'max'}
    _readjust_thinking_params(swapped, 'glm-5.3', '')
    assert swapped['thinking'] == fresh['thinking']
    assert swapped['reasoning_effort'] == fresh['reasoning_effort']
