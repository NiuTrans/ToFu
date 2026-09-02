"""Rootless-container inference and official SWE-bench evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from .dataset import BenchmarkTask


PROJECT_ROOT = Path(__file__).resolve().parents[2]
USER_ROOT = PROJECT_ROOT.parent
EXPERIMENT_ROOT = USER_ROOT / 'tofu-experiment'
UDOCKER_ROOT = Path(os.environ.get(
    'CONTEXT_BENCH_UDOCKER_ROOT',
    str(EXPERIMENT_ROOT / 'udocker_root'),
))
UDOCKER_BIN = UDOCKER_ROOT / 'bin' / 'udocker'
UDOCKER_DIR = UDOCKER_ROOT / '.udocker'
TOFU_RUNTIME = Path(os.environ.get(
    'CONTEXT_BENCH_TOFU_RUNTIME', str(USER_ROOT / 'tbench_runtime' / 'tofu')))
# The dependency/Python snapshot and authored source may advance at different
# rates. A read-only source overlay lets a benchmark run the exact current
# checkout without copying credentials or a multi-gigabyte interpreter tree.
TOFU_SOURCE = Path(os.environ.get(
    'CONTEXT_BENCH_TOFU_SOURCE', str(PROJECT_ROOT)))
TOFU_CONFIG_SOURCE = Path(os.environ.get(
    'CONTEXT_BENCH_TOFU_CONFIG_SOURCE',
    str(PROJECT_ROOT / 'data' / 'config')))
BENCHMARK_MODEL_ID = str(os.environ.get(
    'CONTEXT_BENCH_MODEL_ID', 'gpt-5.6-sol')).strip() or 'gpt-5.6-sol'
BENCHMARK_PROVIDER_ID = str(os.environ.get(
    'CONTEXT_BENCH_PROVIDER_ID', 'oauth_codex')).strip() or 'oauth_codex'
_BENCHMARK_REQUESTED_ALLOW_HOSTS = str(os.environ.get(
    'CONTEXT_BENCH_ALLOW_HOSTS',
    ('chatgpt.com' if BENCHMARK_PROVIDER_ID == 'oauth_codex'
     else 'your-llm-gateway.example.com'))).strip()
# The frozen dispatcher validates every registered route when it builds its
# slot table. OAuth/Codex is provisioned at boot even for a non-OpenAI test,
# so its fixed endpoint must pass that validation without becoming eligible
# for the benchmark's provider-pinned request.
BENCHMARK_ALLOW_HOSTS = ','.join(dict.fromkeys(filter(None, [
    *(host.strip() for host in _BENCHMARK_REQUESTED_ALLOW_HOSTS.split(',')),
    'chatgpt.com',
])))
CODEX_PACKAGE = Path(os.environ.get(
    'CONTEXT_BENCH_CODEX_PACKAGE',
    str(USER_ROOT / 'lib' / 'node_modules' / '@openai' / 'codex'
        / 'node_modules' / '@openai' / 'codex-linux-x64'
        / 'vendor' / 'x86_64-unknown-linux-musl'),
))
CODEX_HOME = Path(os.environ.get(
    'CODEX_HOME', str(USER_ROOT / '.codex')))
LOCK_DIR = Path('/tmp/tofu_context_bench_locks')
LOCK_DIR.mkdir(parents=True, exist_ok=True)
TOFU_DATA_ROOT = Path(os.environ.get(
    'CONTEXT_BENCH_TOFU_DATA_ROOT',
    '/tmp/tofu-context-efficiency-data'))


PUBLIC_GPT56_SOL_PRICING = {
    'inputUsdPerMillion': 5.0,
    'uncachedInputUsdPerMillion': 5.0,
    'cacheReadUsdPerMillion': 0.5,
    'cacheWriteUsdPerMillion': 6.25,
    'outputUsdPerMillion': 30.0,
    'source': 'OpenAI GPT-5.6 Sol public price, frozen 2026-08-10',
}


def _public_pricing_for_model(model_id: str) -> dict:
    """Project the canonical application price table into benchmark fields."""
    if model_id == 'gpt-5.6-sol':
        return dict(PUBLIC_GPT56_SOL_PRICING)
    from lib.pricing import lookup_pricing

    row = lookup_pricing(model_id, prompt_tokens=1)
    if not isinstance(row, dict):
        raise RuntimeError(f'benchmark pricing missing for model {model_id!r}')
    input_rate = float(row.get('input') or 0)
    output_rate = float(row.get('output') or 0)
    cache_read_multiplier = row.get('cacheReadMul')
    cache_write_multiplier = row.get('cacheWriteMul')
    return {
        'inputUsdPerMillion': input_rate,
        'uncachedInputUsdPerMillion': input_rate,
        'cacheReadUsdPerMillion': (
            input_rate * float(
                1 if cache_read_multiplier is None else
                cache_read_multiplier)),
        'cacheWriteUsdPerMillion': (
            input_rate * float(
                1 if cache_write_multiplier is None else
                cache_write_multiplier)),
        'outputUsdPerMillion': output_rate,
        'source': (
            f'Tofu canonical price table for {model_id}, '
            'captured at benchmark launch'),
    }


BENCHMARK_PUBLIC_PRICING = _public_pricing_for_model(BENCHMARK_MODEL_ID)


ARM_CONFIGS: dict[str, dict] = {
    'tofu-control': {
        'cache': {'gpt56BreakpointMode': 'implicit'},
        'tools': {'nativeExposure': 'full', 'programmaticCalling': 'off'},
        'responses': {'promptProfile': 'full'},
        'orchestration': {'multiAgent': 'off'},
        'compaction': {'evidenceLedger': False},
    },
    'tofu-explicit': {
        'cache': {'gpt56BreakpointMode': 'explicit'},
        'tools': {'nativeExposure': 'full', 'programmaticCalling': 'off'},
        'responses': {'promptProfile': 'full'},
        'orchestration': {'multiAgent': 'off'},
        'compaction': {'evidenceLedger': False},
    },
    'tofu-routed': {
        'cache': {'gpt56BreakpointMode': 'implicit'},
        'tools': {'nativeExposure': 'routed', 'programmaticCalling': 'off'},
        'responses': {'promptProfile': 'full'},
        'orchestration': {'multiAgent': 'off'},
        'compaction': {'evidenceLedger': False},
    },
    'tofu-evidence': {
        'cache': {'gpt56BreakpointMode': 'implicit'},
        'tools': {'nativeExposure': 'full', 'programmaticCalling': 'off'},
        'responses': {'promptProfile': 'full'},
        'orchestration': {'multiAgent': 'off'},
        'compaction': {'evidenceLedger': True},
    },
    'tofu-ptc': {
        'cache': {'gpt56BreakpointMode': 'implicit'},
        'tools': {
            'nativeExposure': 'full',
            'programmaticCalling': 'auto',
            'programmaticExposure': 'additive',
        },
        'responses': {'promptProfile': 'full'},
        'orchestration': {'multiAgent': 'off'},
        'compaction': {'evidenceLedger': False},
    },
    # Paired production-like arms for the serial-read adoption policy.  Both
    # keep resident PTC on; the sole independent variable is whether three
    # successful direct read rounds create one gateway-only wire epoch.
    'tofu-ptc-additive': {
        'cache': {'gpt56BreakpointMode': 'explicit'},
        'tools': {
            'nativeExposure': 'routed',
            'programmaticCalling': 'on',
            'programmaticExposure': 'additive',
        },
        'responses': {'promptProfile': 'auto'},
        'orchestration': {'multiAgent': 'off', 'policy': 'v2'},
        'compaction': {'evidenceLedger': False},
    },
    'tofu-ptc-serial-gateway': {
        'cache': {'gpt56BreakpointMode': 'explicit'},
        'tools': {
            'nativeExposure': 'routed',
            'programmaticCalling': 'on',
            'programmaticExposure': 'serial_gateway',
        },
        'responses': {'promptProfile': 'auto'},
        'orchestration': {'multiAgent': 'off', 'policy': 'v2'},
        'compaction': {'evidenceLedger': False},
    },
    'tofu-prompt-lean': {
        'cache': {'gpt56BreakpointMode': 'implicit'},
        'tools': {'nativeExposure': 'full', 'programmaticCalling': 'off'},
        'responses': {'promptProfile': 'lean'},
        'orchestration': {'multiAgent': 'off'},
        'compaction': {'evidenceLedger': False},
    },
    'tofu-effort-medium': {
        'thinkingDepth': 'medium',
        'cache': {'gpt56BreakpointMode': 'implicit'},
        'tools': {'nativeExposure': 'full', 'programmaticCalling': 'off'},
        'responses': {'promptProfile': 'full'},
        'orchestration': {'multiAgent': 'off'},
        'compaction': {'evidenceLedger': False},
    },
    'tofu-effort-low': {
        'thinkingDepth': 'low',
        'cache': {'gpt56BreakpointMode': 'implicit'},
        'tools': {'nativeExposure': 'full', 'programmaticCalling': 'off'},
        'responses': {'promptProfile': 'full'},
        'orchestration': {'multiAgent': 'off'},
        'compaction': {'evidenceLedger': False},
    },
    'tofu-multi-agent': {
        'cache': {'gpt56BreakpointMode': 'implicit'},
        'tools': {'nativeExposure': 'full', 'programmaticCalling': 'off'},
        'responses': {'promptProfile': 'full'},
        'orchestration': {'multiAgent': 'auto'},
        'compaction': {'evidenceLedger': False},
    },
    'tofu-ws64': {
        'cache': {'gpt56BreakpointMode': 'implicit'},
        'tools': {'nativeExposure': 'full', 'programmaticCalling': 'off'},
        'responses': {'promptProfile': 'full'},
        'orchestration': {'multiAgent': 'off'},
        'compaction': {'evidenceLedger': False, 'workingSetTokens': 65536},
    },
    'tofu-ws96': {
        'cache': {'gpt56BreakpointMode': 'implicit'},
        'tools': {'nativeExposure': 'full', 'programmaticCalling': 'off'},
        'responses': {'promptProfile': 'full'},
        'orchestration': {'multiAgent': 'off'},
        'compaction': {'evidenceLedger': False, 'workingSetTokens': 98304},
    },
    'tofu-ws128': {
        'cache': {'gpt56BreakpointMode': 'implicit'},
        'tools': {'nativeExposure': 'full', 'programmaticCalling': 'off'},
        'responses': {'promptProfile': 'full'},
        'orchestration': {'multiAgent': 'off'},
        'compaction': {'evidenceLedger': False, 'workingSetTokens': 131072},
    },
}


@dataclass
class InferenceOutcome:
    patch: str = ''
    usage: dict = field(default_factory=dict)
    round_usage: list[dict] = field(default_factory=list)
    prefix_fingerprints: list[dict] = field(default_factory=list)
    context_telemetry: dict = field(default_factory=dict)
    compactions: list[dict] = field(default_factory=list)
    latency_ms: int = 0
    provider_id: str = ''
    task_id: str = ''
    api_rounds: int = 0
    tool_calls: int = 0
    content: str = ''
    error: str = ''
    infrastructure_error: bool = False


@dataclass
class EvaluationOutcome:
    resolved: bool = False
    patch_applies: bool = False
    fail_to_pass_passed: int = 0
    fail_to_pass_total: int = 0
    pass_to_pass_passed: int = 0
    pass_to_pass_total: int = 0
    duration_ms: int = 0
    error: str = ''
    infrastructure_error: bool = False


def build_agent_prompt(task: BenchmarkTask, *, allow_subagents: bool = False) -> str:
    agent_rule = (
        'Use native read-only subagents only for genuinely independent work; '
        'the root agent alone may edit files and owns the final verification.'
        if allow_subagents else
        'Work alone; do not create or delegate to sub-agents.'
    )
    prompt = f"""You are solving a GitHub issue in repository {task.repo}.

## Issue Description

{task.problem_statement}

## Instructions

1. {agent_rule}
2. Do not use the network. The repository and dependencies are already present.
3. Read the relevant source and tests, identify the root cause, and make the minimal fix.
4. Do not modify or add tests.
5. Run focused existing tests when useful, then inspect the final diff.
6. Finish only after the working tree contains the intended source-code fix.
"""
    if task.hints_text:
        prompt += f'\n## Hints\n\n{task.hints_text}\n'
    return prompt


def arm_config(arm: str, candidate_path: Path | None = None) -> dict:
    if arm == 'codex-mechanism' or arm == 'codex-product':
        return {}
    if arm == 'tofu-candidate':
        if candidate_path is None:
            raise ValueError('tofu-candidate requires --candidate-config')
        payload = json.loads(candidate_path.read_text())
        return dict(payload.get('config') or payload)
    if arm not in ARM_CONFIGS:
        raise ValueError(f'unknown arm: {arm}')
    return json.loads(json.dumps(ARM_CONFIGS[arm]))


def _udocker(*args: str, timeout: int = 1800,
             merge_output: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env['UDOCKER_DIR'] = str(UDOCKER_DIR)
    stderr = subprocess.STDOUT if merge_output else subprocess.PIPE
    return subprocess.run(
        [str(UDOCKER_BIN), *args], env=env,
        stdout=subprocess.PIPE, stderr=stderr, text=True, timeout=timeout,
    )


def _container_name(task: BenchmarkTask) -> str:
    safe = task.instance_id.replace('/', '-').replace('.', '-')
    return f'ctxeff-{safe}'


def _container_exists(name: str) -> bool:
    path = UDOCKER_DIR / 'containers' / name
    return path.exists() or path.is_symlink()


def _image_tag_dir(image: str) -> Path:
    namespace, name_tag = image.split('/', 1)
    name, _, tag = name_tag.partition(':')
    return UDOCKER_DIR / 'repos' / namespace / name / (tag or 'latest')


def _fix_layer_links(image: str) -> None:
    tag_dir = _image_tag_dir(image)
    manifest_path = tag_dir / 'manifest'
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return
    entries = list(manifest.get('layers') or [])
    if manifest.get('config'):
        entries.append(manifest['config'])
    for entry in entries:
        digest = entry.get('digest') or ''
        source = UDOCKER_DIR / 'layers' / digest
        target = tag_dir / digest
        if digest and source.exists() and not target.exists():
            try:
                target.symlink_to(source)
            except OSError:
                pass


def _image_snapshot_complete(image: str) -> bool:
    """Return whether every manifest-owned config/layer blob is present."""
    manifest_path = _image_tag_dir(image) / 'manifest'
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return False
    entries = list(manifest.get('layers') or [])
    if manifest.get('config'):
        entries.append(manifest['config'])
    digests = [
        str(entry.get('digest') or '') for entry in entries
        if isinstance(entry, dict) and entry.get('digest')
    ]
    return bool(digests) and all(
        (UDOCKER_DIR / 'layers' / digest).exists() for digest in digests)


def ensure_container(task: BenchmarkTask, *, timeout: int = 2400) -> tuple[str, str]:
    if not UDOCKER_BIN.is_file():
        return '', f'udocker is missing at {UDOCKER_BIN}'
    name = _container_name(task)
    if _container_exists(name):
        return name, ''
    if not _image_snapshot_complete(task.image):
        pulled = _udocker('pull', task.image, timeout=timeout)
        if pulled.returncode != 0:
            return '', 'image pull/repair failed: ' + (
                pulled.stderr or pulled.stdout or '')[-1000:]
        if not _image_snapshot_complete(task.image):
            return '', 'image pull completed without every manifest layer'
    _fix_layer_links(task.image)
    created = _udocker('create', f'--name={name}', task.image, timeout=timeout)
    if created.returncode != 0 and not _container_exists(name):
        return '', 'container create failed: ' + (
            created.stderr or created.stdout or '')[-1000:]
    return name, ''


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _proxy_env_flags(extra: dict[str, str] | None = None) -> list[str]:
    flags: list[str] = []
    for key in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'):
        value = os.environ.get(key, '')
        if value:
            flags.extend(['--env', f'{key}={value}'])
    no_proxy = os.environ.get('no_proxy') or os.environ.get('NO_PROXY') or ''
    provider_hosts = {
        host.strip().lower() for host in BENCHMARK_ALLOW_HOSTS.split(',')
        if host.strip()
    }
    no_proxy_entries = []
    for entry in no_proxy.split(','):
        normalized = entry.strip().lower().lstrip('.')
        if not normalized:
            continue
        # The disposable container has no general DNS resolver.  A host-level
        # NO_PROXY suffix for the selected provider would force direct DNS and
        # bypass the authenticated benchmark proxy, so remove just that scope.
        if any(host == normalized or host.endswith('.' + normalized)
               for host in provider_hosts):
            continue
        no_proxy_entries.append(entry.strip())
    no_proxy = ','.join(dict.fromkeys([
        *no_proxy_entries, '127.0.0.1', 'localhost']))
    flags.extend(['--env', f'no_proxy={no_proxy}', '--env', f'NO_PROXY={no_proxy}'])
    for key, value in (extra or {}).items():
        flags.extend(['--env', f'{key}={value}'])
    return flags


_PROOT_SHIM = r'''
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
if [ -d /hosttmp ]; then export TMPDIR=/hosttmp; fi
mkdir -p "${TMPDIR:-/tmp}" 2>/dev/null
SITEPY_DIR="$(mktemp -d /hosttmp/ctxeff-site.XXXXXX 2>/dev/null || mktemp -d)"
cat > "$SITEPY_DIR/sitecustomize.py" << 'SITEPY'
import functools as _ft, os as _os, tempfile as _tf
_tf.TemporaryFile = _ft.partial(_tf.NamedTemporaryFile, delete=False)
_orig_rollover = _tf.SpooledTemporaryFile.rollover
def _safe_rollover(self):
    if getattr(self, '_rolled', False): return
    try: return _orig_rollover(self)
    except (FileNotFoundError, OSError): return None
_tf.SpooledTemporaryFile.rollover = _safe_rollover
SITEPY
export PYTHONPATH="$SITEPY_DIR:${PYTHONPATH:-}"
'''


_TOFU_DRIVER = r'''#!/usr/bin/env python3
import json, os, time, urllib.request, urllib.error

def request(method, url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='replace')[:2000]
        raise RuntimeError('HTTP ' + str(exc.code) + ': ' + detail) from exc

def clean_usage(usage):
    if not isinstance(usage, dict): return {}
    keys = ('prompt_tokens','completion_tokens','total_tokens',
            'cache_read_tokens','cache_write_tokens','input_tokens',
            'output_tokens','reasoning_tokens','stream_elapsed_ms',
            'prompt_tokens_details','completion_tokens_details','_wire_static')
    out = {key: usage[key] for key in keys if key in usage}
    dispatch = usage.get('_dispatch') or {}
    if isinstance(dispatch, dict):
        out['_dispatch'] = {key: dispatch.get(key) for key in
            ('provider_id','model','latency_ms','attempt','429_retries')
            if key in dispatch}
    return out

def task_state(base, task_id):
    if not task_id: return {}
    response = request('GET', base + '/api/v1/tasks/' + task_id, timeout=90)
    return response.get('task') or response.get('result') or response

def recover_task_id(base, conv_id):
    """Recover a task created before the blocking facade returned HTTP 500.

    The agent task is registered and persisted before ``agent/run`` waits for
    it.  At the benchmark deadline that facade can time out without returning
    its task id, even though the task snapshot still contains every completed
    round and its usage.  The conversation index is the stable recovery key.
    """
    response = request('GET', base + '/api/v1/tasks/by-conv/' + conv_id,
                       timeout=90)
    result = response.get('result') or response
    tasks = result.get('tasks') or [] if isinstance(result, dict) else []
    if not tasks: return ''
    return str(tasks[0].get('taskId') or tasks[0].get('id') or '')

def build_result(payload, state, task_id, request_error=''):
    payload = payload if isinstance(payload, dict) else {}
    state = state if isinstance(state, dict) else {}
    rounds = []
    prefixes = []
    for item in state.get('apiRounds',[]):
        usage = clean_usage(item.get('usage') or {})
        rounds.append({'round':item.get('round'),'model':item.get('model'),
                       'usage':usage,'cost':item.get('cost') or {}})
        if usage.get('_wire_static'):
            prefixes.append({'round':item.get('round'),
                             'fingerprint':usage['_wire_static']})
    events = state.get('events',[])
    compactions = []
    for event in events:
        kind = str(event.get('type') or event.get('phase') or '').lower()
        if 'compact' in kind:
            compactions.append({k:v for k,v in event.items()
                                if k not in ('messages','content','thinking')})
    status = str(payload.get('status') or state.get('status') or '')
    error = payload.get('error') or state.get('error') or request_error or ''
    response_items = []
    for item in (state.get('_responsesItems') or []):
        if isinstance(item, dict): response_items.append(item)
    for tool_round in (state.get('toolRounds') or []):
        if not isinstance(tool_round, dict): continue
        for item in (tool_round.get('_responsesItems') or []):
            if isinstance(item, dict): response_items.append(item)
    multi_agent_ids = {
        str(item.get('id') or item.get('call_id') or index)
        for index, item in enumerate(response_items)
        if item.get('type') == 'multi_agent_call'
    }
    return {
      'status':status,
      'error':str(error or ''),
      'content':str(payload.get('content') or state.get('content') or '')[:20000],
      'task_id':task_id,
      'provider_id':str(state.get('provider_id') or payload.get('provider_id') or ''),
      'usage':clean_usage(payload.get('usage') or state.get('usage') or {}),
      'n_tool_rounds':int(payload.get('n_tool_rounds') or len(state.get('toolRounds') or [])),
      'round_usage':rounds,
      'prefix_fingerprints':prefixes,
      'context_telemetry':{
          'rounds':state.get('_contextTelemetryRounds') or [],
          'toolExposure':state.get('_toolExposureTelemetry') or {},
          'optimizationDecisions':state.get(
              '_toolOrchestrationDecisions') or state.get(
                  '_gpt56OptimizationDecisions') or [],
          'multiAgentCalls':len(multi_agent_ids),
          'programRuns':[
              {
                'callId':run.get('callId'),
                'source':run.get('source'),
                'status':run.get('status'),
                'childCallCount':len(run.get('childCalls') or []),
                'admittedCallCount':len(run.get('admittedCallIds') or []),
                'rejectedCallCount':(
                    len(run.get('rejectedCallIds') or []) +
                    int(run.get('duplicateRejectedCallCount') or 0)),
                'continuationCount':int(run.get('continuationCount') or 0),
                'rawOutputBytes':int(run.get('rawOutputBytes') or 0),
                'outputBytes':int(run.get('outputBytes') or 0),
                'outputTruncated':bool(run.get('outputTruncated')),
                'limits':run.get('limits') or {},
              }
              for run in (state.get('programRuns') or [])
              if isinstance(run,dict)
          ],
      },
      'compactions':compactions,
    }

base = os.environ['TOFU_LOCAL_URL'].rstrip('/')
prompt = open('/host/prompt.txt', encoding='utf-8').read()
config = json.load(open('/host/config.json', encoding='utf-8'))
benchmark_model_id = os.environ['BENCHMARK_MODEL_ID']
benchmark_provider_id = os.environ['BENCHMARK_PROVIDER_ID']
routing_snapshot = request('GET', base + '/api/v1/model-routing', timeout=90)
routing_document = routing_snapshot.get('model_routing') or {}
routing_models = routing_document.get('models') or []
routing_offerings = routing_document.get('offerings') or []
routing_accesses = routing_document.get('provider_accesses') or []
provider_by_access = {
    str(item.get('provider_access_id') or ''):
        str(item.get('provider_id') or '')
    for item in routing_accesses if isinstance(item,dict)
}
model_ref = next((
    {'creator_id':str(item.get('creator_id') or ''),
     'model_id':str(item.get('model_id') or '')}
    for item in routing_models
    if isinstance(item,dict) and item.get('model_id') == benchmark_model_id
), None)
if model_ref is None:
    # A provider-scoped offering is a first-class model reference in routing
    # v2.  Fresh isolated authorities can deliberately retain an offering
    # while the optional creator catalog has not yet reconciled it.
    model_ref = next((
        {'provider_id':provider_by_access.get(
             str(item.get('provider_access_id') or ''), ''),
         'offering_id':str(item.get('offering_id') or '')}
        for item in routing_offerings
        if isinstance(item,dict)
        and provider_by_access.get(
            str(item.get('provider_access_id') or '')) == benchmark_provider_id
        and (
            item.get('pending_model_id') == benchmark_model_id
            or (isinstance(item.get('model'),dict)
                and item['model'].get('model_id') == benchmark_model_id))
    ), None)
if model_ref is None:
    registered = [
        str(item.get('creator_id') or '') + '/' +
        str(item.get('model_id') or '')
        for item in routing_models if isinstance(item,dict)
    ]
    registered += [
        provider_by_access.get(
            str(item.get('provider_access_id') or ''), '') + ':' +
        str(item.get('pending_model_id') or
            ((item.get('model') or {}).get('model_id')
             if isinstance(item.get('model'),dict) else '') or '')
        for item in routing_offerings if isinstance(item,dict)
    ]
    open('/host/inference.json','w',encoding='utf-8').write(json.dumps({
        'status':'error',
        'error':'benchmark model is absent from routing authority: ' +
                benchmark_model_id + '; registered=' +
                json.dumps(registered[:120]),
    },ensure_ascii=False))
    raise SystemExit(3)
body = {
  'model':model_ref,
  'routing':{'preferred_provider_id':benchmark_provider_id},
  'messages':[{'role':'user','content':prompt}],
  'config':config,
  'stream':False,
  'timeout_s':float(os.environ.get('AGENT_TIMEOUT_S','1800')),
  'conversation_id':os.environ['CONVERSATION_ID'],
}
result = {'status':'error','error':'driver did not complete'}
try:
    response = request('POST', base + '/api/v1/agent/run', body,
                       int(float(body['timeout_s'])) + 90)
    payload = response.get('result') if isinstance(response.get('result'),dict) else response
    task_id = payload.get('task_id') or ''
    state = task_state(base, task_id)
    result = build_result(payload, state, task_id)
except Exception as exc:
    # A blocking ``agent/run`` timeout used to erase all cost telemetry even
    # though the underlying task and completed API rounds remained available.
    # Recover the task by the request-scoped conversation id before the local
    # benchmark server is stopped.  If recovery itself fails, the reader marks
    # the attempt as infrastructure-invalid and the fixed retry policy applies.
    registered_model_ids = [
        str(item.get('creator_id') or '') + '/' +
        str(item.get('model_id') or '')
        for item in routing_models if isinstance(item,dict)
    ]
    request_error = (type(exc).__name__ + ': ' + str(exc) +
                     '; registered_models=' +
                     json.dumps(registered_model_ids[:80]))
    try:
        task_id = recover_task_id(base, os.environ['CONVERSATION_ID'])
        state = task_state(base, task_id)
        result = build_result({}, state, task_id, request_error)
    except Exception as recovery_exc:
        result = {
          'status':'error',
          'error':request_error + '; recovery=' +
                  type(recovery_exc).__name__ + ': ' + str(recovery_exc),
        }
open('/host/inference.json','w',encoding='utf-8').write(
    json.dumps(result,ensure_ascii=False))
'''


def _base_reset_and_diff(task: BenchmarkTask) -> tuple[str, str]:
    reset = (
        f'cd /testbed && git reset --hard {shlex.quote(task.base_commit)} '
        '>/dev/null 2>&1 && git clean -fd >/dev/null 2>&1; '
        f'export CTXE_BASE_SHA={shlex.quote(task.base_commit)}'
    )
    # Stage tracked edits plus intentional, non-ignored new source files.  A
    # blanket ``git add -A`` also captures compiler scratch trees (observed as
    # .tmp/rustc*/symbols.o), journals and agent state.  Binary scratch files
    # then make an otherwise valid model patch impossible to apply.
    untracked = r'''git ls-files --others --exclude-standard -z | while IFS= read -r -d '' CTXE_PATH; do
  case "$CTXE_PATH" in
    .tofu/*|.chatui/*|.tmp/*|.cache/*|.config/*|JOURNAL.md) ;;
    *) git add -- "$CTXE_PATH" ;;
  esac
done'''
    excludes = (
        "':(exclude).tofu' ':(exclude).chatui' ':(exclude).tmp' "
        "':(exclude).cache' ':(exclude).config' ':(exclude)JOURNAL.md'"
    )
    diff = (
        'cd /testbed && git reset -q 2>/dev/null; '
        'git add -u 2>/dev/null; '
        f'{untracked}; '
        f'git diff --cached --no-color "$CTXE_BASE_SHA" -- . {excludes} '
        '> /host/model_patch.diff 2>/dev/null || true'
    )
    return reset, diff


def _read_inference(run_dir: Path, started: float,
                    process: subprocess.CompletedProcess | None,
                    *, backend: str) -> InferenceOutcome:
    outcome = InferenceOutcome(latency_ms=int((time.time() - started) * 1000))
    patch_path = run_dir / 'model_patch.diff'
    if patch_path.exists():
        outcome.patch = patch_path.read_text(errors='replace')
    data_path = run_dir / 'inference.json'
    if data_path.exists():
        try:
            data = json.loads(data_path.read_text())
        except ValueError as exc:
            outcome.error = f'invalid inference metadata: {exc}'
            outcome.infrastructure_error = True
            return outcome
        outcome.usage = data.get('usage') or {}
        outcome.round_usage = data.get('round_usage') or []
        outcome.prefix_fingerprints = data.get('prefix_fingerprints') or []
        outcome.context_telemetry = data.get('context_telemetry') or {}
        outcome.compactions = data.get('compactions') or []
        outcome.provider_id = str(data.get('provider_id') or '')
        outcome.task_id = str(data.get('task_id') or '')
        outcome.api_rounds = len(outcome.round_usage)
        outcome.tool_calls = int(data.get('n_tool_rounds') or 0)
        outcome.content = str(data.get('content') or '')
        outcome.error = str(data.get('error') or '')
        status = str(data.get('status') or '')
        if status not in ('done', 'completed', '') and not outcome.error:
            outcome.error = f'agent status={status}'
    else:
        tail = '' if process is None else (process.stderr or process.stdout or '')[-1500:]
        outcome.error = f'{backend} inference metadata missing: {tail}'
        outcome.infrastructure_error = True
    if process is not None and process.returncode != 0 and not outcome.patch:
        outcome.infrastructure_error = True
    # Missing usage makes the economic experiment invalid even if a partial
    # working-tree diff exists.  Treat it as infrastructure so the one fixed
    # retry is used; an agent timeout with a recovered usage snapshot remains
    # a genuine (non-retried) agent outcome.
    metered_tokens = sum(int(outcome.usage.get(key) or 0) for key in (
        'prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens',
        'total_tokens'))
    if outcome.error and metered_tokens <= 0:
        outcome.infrastructure_error = True
    return outcome


def _prepare_tofu_data(run_dir: Path) -> Path:
    """Create an isolated local data directory for one inference attempt."""
    key = hashlib.sha256(str(run_dir.resolve()).encode()).hexdigest()[:20]
    data_dir = TOFU_DATA_ROOT / key
    config_candidates = (
        TOFU_CONFIG_SOURCE,
        TOFU_RUNTIME / 'chatui' / 'data' / 'config',
        TOFU_RUNTIME / 'tofu' / 'data' / 'config',
    )
    config_source = next(
        (path for path in config_candidates if path.is_dir()), None)
    config_target = data_dir / 'config'
    data_dir.mkdir(parents=True, exist_ok=True)
    if config_source is not None:
        shutil.copytree(config_source, config_target, dirs_exist_ok=True)
        _project_benchmark_server_config(config_target / 'server_config.json')
    return data_dir


def _project_benchmark_server_config(config_path: Path) -> None:
    """Bound a disposable migration to the selected provider/model.

    The production config may contain hundreds of cached catalog entries.
    Copying that entire legacy catalog into every fresh benchmark authority is
    both irrelevant to a single-route experiment and can exceed the sidecar's
    bounded receipt size.  Subscription routes bootstrap outside this legacy
    provider list, so their established path remains untouched.
    """
    if BENCHMARK_PROVIDER_ID in {'oauth_codex',
                                 'chatgpt_codex_subscription'}:
        return
    try:
        config = json.loads(config_path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(config, dict):
        return
    providers = config.get('providers')
    if not isinstance(providers, list):
        return
    selected = [
        provider for provider in providers
        if isinstance(provider, dict)
        and str(provider.get('id') or provider.get('key') or '') ==
        BENCHMARK_PROVIDER_ID
    ]
    if not selected:
        raise RuntimeError(
            f'benchmark provider missing from config: '
            f'{BENCHMARK_PROVIDER_ID!r}')
    for provider in selected:
        models = provider.get('models')
        if isinstance(models, list):
            provider['models'] = [
                model for model in models
                if isinstance(model, dict)
                and str(model.get('model_id') or model.get('id') or '') ==
                BENCHMARK_MODEL_ID
            ]
            if not provider['models']:
                raise RuntimeError(
                    f'benchmark model {BENCHMARK_MODEL_ID!r} missing from '
                    f'provider {BENCHMARK_PROVIDER_ID!r}')
    config['providers'] = selected
    catalog = config.get('model_catalog')
    if isinstance(catalog, dict):
        models = catalog.get('models')
        if isinstance(models, dict):
            catalog['models'] = {
                key: value for key, value in models.items()
                if key == BENCHMARK_MODEL_ID
            }
        offerings = catalog.get('offerings')
        if isinstance(offerings, dict):
            catalog['offerings'] = {
                key: value for key, value in offerings.items()
                if isinstance(value, dict)
                and value.get('provider_id') == BENCHMARK_PROVIDER_ID
                and value.get('model_id') == BENCHMARK_MODEL_ID
            }
        routes = catalog.get('routes')
        if isinstance(routes, dict):
            catalog['routes'] = {
                key: value for key, value in routes.items()
                if key == BENCHMARK_MODEL_ID
            }
    config['model_defaults'] = {
        'default_model': BENCHMARK_MODEL_ID,
        'fallback_model': BENCHMARK_MODEL_ID,
    }
    bypass_domains = config.get('proxy_bypass_domains')
    if isinstance(bypass_domains, list):
        provider_hosts = {
            host for provider in selected
            if (host := (urlparse(
                str(provider.get('base_url') or '')).hostname or '').lower())
        }
        config['proxy_bypass_domains'] = [
            domain for domain in bypass_domains
            if not any(
                host == str(domain).strip().lower().lstrip('.')
                or host.endswith(
                    '.' + str(domain).strip().lower().lstrip('.'))
                for host in provider_hosts)
        ]
    config_path.write_text(json.dumps(
        config, ensure_ascii=False, separators=(',', ':')))


def run_tofu(task: BenchmarkTask, run_dir: Path, config: dict, *,
             timeout_s: int = 1800) -> InferenceOutcome:
    run_dir.mkdir(parents=True, exist_ok=True)
    container, error = ensure_container(task)
    if error:
        return InferenceOutcome(error=error, infrastructure_error=True)
    if not TOFU_RUNTIME.is_dir():
        return InferenceOutcome(
            error=f'Tofu runtime missing: {TOFU_RUNTIME}',
            infrastructure_error=True)
    if not TOFU_SOURCE.is_dir():
        return InferenceOutcome(
            error=f'Tofu source missing: {TOFU_SOURCE}',
            infrastructure_error=True)
    orchestration_cfg = (
        config.get('orchestration') if isinstance(config, dict) else {})
    allow_subagents = (
        isinstance(orchestration_cfg, dict)
        and orchestration_cfg.get('multiAgent') not in (None, 'off'))
    prompt = build_agent_prompt(
        task, allow_subagents=allow_subagents,
    ) + '\n<!-- arm-isolation: ' + run_dir.name + ' -->\n'
    (run_dir / 'prompt.txt').write_text(prompt)
    cfg = {
        'model': BENCHMARK_MODEL_ID,
        'thinkingDepth': 'xhigh',
        'projectPath': '/testbed',
        'agentBackend': 'builtin',
        'searchMode': 'off',
        'fetchEnabled': False,
        'mcpEnabled': False,
        'memoryEnabled': False,
        'preferencesEnabled': False,
        'swarmEnabled': False,
        'disableModelFallback': True,
        **config,
    }
    (run_dir / 'config.json').write_text(json.dumps(cfg, ensure_ascii=False))
    (run_dir / 'tofu_driver.py').write_text(_TOFU_DRIVER)
    tofu_data_dir = _prepare_tofu_data(run_dir)
    port = _free_port()
    reset, diff = _base_reset_and_diff(task)
    script = f'''set +e
{_PROOT_SHIM}
{reset}
export PATH=/opt/agent/python/bin:/opt/agent/bin:$PATH
export PYTHONPATH=/opt/agent/venv/lib/python3.11/site-packages:${{PYTHONPATH:-}}
export PYTHONUNBUFFERED=1
cd /opt/agent/tofu
export TOFU_BYO_ALLOW_HOSTS={shlex.quote(BENCHMARK_ALLOW_HOSTS)}
nohup /opt/agent/python/bin/python3 server.py --host 127.0.0.1 --port {port} --no-tls \
  > /host/tofu_server.log 2>&1 &
SERVER_PID=$!
UP=0
for i in $(seq 1 120); do
  if /opt/agent/python/bin/python3 -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/api/v1/capabilities', timeout=2).read(1)" \
    >/dev/null 2>&1; then UP=1; break; fi
  sleep 1
done
if [ "$UP" = 1 ]; then
  cd /testbed
  /opt/agent/python/bin/python3 /host/tofu_driver.py > /host/driver.log 2>&1
  DRIVER_RC=$?
else
  echo '{{"status":"error","error":"Tofu server boot timeout"}}' > /host/inference.json
  DRIVER_RC=90
fi
kill "$SERVER_PID" 2>/dev/null || true
{diff}
exit "$DRIVER_RC"
'''
    (run_dir / 'run.sh').write_text(script)
    extra = {
        'TOFU_LOCAL_URL': f'http://127.0.0.1:{port}',
        'AGENT_TIMEOUT_S': str(timeout_s),
        'CONVERSATION_ID': f'ctxeff-{task.instance_id}-{run_dir.name}',
        'TOFU_SKIP_LOCK': '1',
        # The benchmark owns the isolated worker process and its teardown.
        # Bypass the personal-install lifecycle manager; starting another
        # daemon inside the disposable rootless container would outlive the
        # harness boundary and cannot acquire its host supervisor contract.
        'TOFU_SERVER_WORKER': '1',
        # The benchmark already limits each task to one agent and runs inside
        # an external container.  Shared-host pressure remains telemetry, but
        # must not turn every arm into an HTTP 503 before inference starts.
        'TOFU_ADMISSION_CGROUP_PCT': '0',
        'TOFU_SUBSCRIPTION_TRUST_CONFIGURED_PROXY': '1',
        # The disposable benchmark container intentionally has no general DNS
        # path. The selected subscription endpoint is fixed by routing above;
        # admit that exact hostname so the SSRF guard can validate the route
        # before provider egress proceeds through the configured proxy.
        'TOFU_BYO_ALLOW_HOSTS': BENCHMARK_ALLOW_HOSTS,
        'BENCHMARK_MODEL_ID': BENCHMARK_MODEL_ID,
        'BENCHMARK_PROVIDER_ID': BENCHMARK_PROVIDER_ID,
    }
    cmd = [
        'run', *_proxy_env_flags(extra),
        '-v', f'{run_dir.resolve()}:/host',
        '-v', f'{TOFU_RUNTIME.resolve()}:/opt/agent',
        '-v', f'{TOFU_SOURCE.resolve()}:/opt/agent/tofu',
        '-v', f'{tofu_data_dir.resolve()}:/opt/agent/tofu/data',
        '-v', '/tmp:/hosttmp',
        container, 'bash', '/host/run.sh',
    ]
    started = time.time()
    try:
        process = _udocker(*cmd, timeout=timeout_s + 300)
    except subprocess.TimeoutExpired:
        outcome = _read_inference(run_dir, started, None, backend='Tofu')
        if not outcome.error:
            outcome.error = f'Tofu timeout after {timeout_s}s'
        outcome.infrastructure_error = True
        return outcome
    return _read_inference(run_dir, started, process, backend='Tofu')


def _codex_usage(events: list[dict]) -> tuple[dict, int, str]:
    usage = {}
    tool_calls = 0
    error = ''
    for event in events:
        kind = event.get('type')
        if kind == 'turn.completed':
            raw = event.get('usage') or {}
            input_tokens = int(raw.get('input_tokens') or 0)
            cached = int(raw.get('cached_input_tokens') or 0)
            output = int(raw.get('output_tokens') or 0)
            reasoning = int(raw.get('reasoning_output_tokens') or 0)
            usage = {
                'prompt_tokens': input_tokens,
                'completion_tokens': output,
                'cache_read_tokens': cached,
                'cache_write_tokens': 0,
                'reasoning_tokens': reasoning,
                'total_tokens': input_tokens + output,
            }
        elif kind == 'turn.failed':
            error = str(event.get('error') or event)
        elif kind == 'error':
            error = str(event.get('message') or event.get('error') or event)
        elif kind == 'item.completed':
            item = event.get('item') or {}
            if item.get('type') in (
                    'command_execution', 'file_change', 'mcp_tool_call',
                    'web_search'):
                tool_calls += 1
    return usage, tool_calls, error


def run_codex(task: BenchmarkTask, run_dir: Path, *, product: bool = False,
              timeout_s: int = 1800) -> InferenceOutcome:
    run_dir.mkdir(parents=True, exist_ok=True)
    container, error = ensure_container(task)
    if error:
        return InferenceOutcome(error=error, infrastructure_error=True)
    codex_bin = CODEX_PACKAGE / 'bin' / 'codex'
    if not codex_bin.is_file() or not (CODEX_HOME / 'auth.json').is_file():
        return InferenceOutcome(
            error='Codex package or authentication is missing',
            infrastructure_error=True)
    (run_dir / 'prompt.txt').write_text(build_agent_prompt(task))
    reset, diff = _base_reset_and_diff(task)
    config_flags = '' if product else (
        '--ignore-user-config --ignore-rules '
        '-c model_reasoning_effort=\'"xhigh"\' '
    )
    script = f'''set +e
{_PROOT_SHIM}
{reset}
export CODEX_HOME=/opt/codex-home
export PATH=/opt/codex/codex-path:$PATH
cd /testbed
timeout --kill-after=30 {int(timeout_s)} /opt/codex/bin/codex exec \
  --json --ephemeral {config_flags}-m gpt-5.6-sol \
  --sandbox danger-full-access -C /testbed - < /host/prompt.txt \
  > /host/codex_events.jsonl 2> /host/codex_stderr.log
CODEX_RC=$?
{diff}
exit "$CODEX_RC"
'''
    (run_dir / 'run.sh').write_text(script)
    cmd = [
        'run', *_proxy_env_flags(),
        '-v', f'{run_dir.resolve()}:/host',
        '-v', f'{CODEX_PACKAGE.resolve()}:/opt/codex',
        '-v', f'{CODEX_HOME.resolve()}:/opt/codex-home',
        '-v', '/tmp:/hosttmp',
        container, 'bash', '/host/run.sh',
    ]
    started = time.time()
    try:
        process = _udocker(*cmd, timeout=timeout_s + 120)
    except subprocess.TimeoutExpired:
        return InferenceOutcome(
            patch=(run_dir / 'model_patch.diff').read_text(errors='replace')
            if (run_dir / 'model_patch.diff').exists() else '',
            latency_ms=int((time.time() - started) * 1000),
            error=f'Codex timeout after {timeout_s}s',
            infrastructure_error=True,
        )
    events = []
    events_path = run_dir / 'codex_events.jsonl'
    if events_path.exists():
        for line in events_path.read_text(errors='replace').splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    usage, tool_calls, event_error = _codex_usage(events)
    patch = ((run_dir / 'model_patch.diff').read_text(errors='replace')
             if (run_dir / 'model_patch.diff').exists() else '')
    stderr = ((run_dir / 'codex_stderr.log').read_text(errors='replace')[-2000:]
              if (run_dir / 'codex_stderr.log').exists() else '')
    error = event_error
    if process.returncode != 0 and not error:
        error = f'Codex exit {process.returncode}: {stderr}'
    return InferenceOutcome(
        patch=patch,
        usage=usage,
        round_usage=[{'round': 1, 'model': 'gpt-5.6-sol', 'usage': usage}],
        latency_ms=int((time.time() - started) * 1000),
        provider_id='chatgpt_codex_subscription',
        api_rounds=1 if usage else 0,
        tool_calls=tool_calls,
        error=error,
        infrastructure_error=bool(error and not usage and not patch),
    )


def _evaluation_infrastructure_error(output: str) -> str:
    """Recognize failures that prevent the task's tests from starting.

    Keep this deliberately narrow: a product test may legitimately exercise
    network failures, while a missing build-runner bootstrap is benchmark
    infrastructure and must not be charged to either model arm.
    """
    normalized = str(output or '').lower()
    gradle_wrapper_download_failed = (
        'could not download gradle-wrapper.jar' in normalized
        and 'unknownhostexception' in normalized
    )
    if gradle_wrapper_download_failed:
        return (
            'evaluation bootstrap failed: Gradle wrapper dependency was '
            'unavailable in the network-isolated grader'
        )
    return ''


def evaluate_patch(task: BenchmarkTask, patch: str, run_dir: Path, *,
                   timeout_s: int = 1800) -> EvaluationOutcome:
    started = time.time()
    if not patch.strip():
        return EvaluationOutcome(
            error='agent produced no patch',
            duration_ms=int((time.time() - started) * 1000),
        )
    container, error = ensure_container(task)
    if error:
        return EvaluationOutcome(error=error, infrastructure_error=True)
    eval_dir = run_dir / 'eval'
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / 'patch.diff').write_text(patch)
    (eval_dir / 'eval.sh').write_text(task.eval_script)
    script = f'''set +e
{_PROOT_SHIM}
cd /testbed
git reset --hard {shlex.quote(task.base_commit)} >/dev/null 2>&1
git clean -fd >/dev/null 2>&1
APPLIED=0
for FLAGS in '--verbose' '--verbose --3way' '--verbose --reject'; do
  if git apply $FLAGS /host/patch.diff > /host/patch_apply.log 2>&1; then APPLIED=1; break; fi
done
if [ "$APPLIED" != 1 ] || find /testbed -name '*.rej' -print -quit | grep -q .; then
  echo '>>>>> Patch Apply Failed'
  touch /host/patch_apply_failed.flag
  exit 2
fi
echo '>>>>> Applied Patch'
chmod +x /host/eval.sh
bash /host/eval.sh
'''
    (eval_dir / 'run.sh').write_text(script)
    cmd = [
        'run', *_proxy_env_flags(),
        '-v', f'{eval_dir.resolve()}:/host',
        '-v', '/tmp:/hosttmp',
        container, 'bash', '/host/run.sh',
    ]
    try:
        process = _udocker(*cmd, timeout=timeout_s, merge_output=True)
    except subprocess.TimeoutExpired:
        return EvaluationOutcome(
            error=f'evaluation timeout after {timeout_s}s',
            infrastructure_error=True,
            duration_ms=int((time.time() - started) * 1000),
        )
    output = process.stdout or ''
    log_path = eval_dir / 'test_output.txt'
    log_path.write_text(output)
    if (eval_dir / 'patch_apply_failed.flag').exists():
        detail = ((eval_dir / 'patch_apply.log').read_text(errors='replace')[-1000:]
                  if (eval_dir / 'patch_apply.log').exists() else '')
        return EvaluationOutcome(
            error='patch did not apply: ' + detail,
            duration_ms=int((time.time() - started) * 1000),
        )
    infrastructure_error = _evaluation_infrastructure_error(output)
    if infrastructure_error:
        return EvaluationOutcome(
            error=infrastructure_error,
            infrastructure_error=True,
            patch_applies=True,
            duration_ms=int((time.time() - started) * 1000),
        )
    try:
        from swebench.harness.grading import get_eval_report
        from swebench.harness.test_spec.test_spec import TestSpec
        spec = TestSpec(
            instance_id=task.instance_id,
            repo=task.repo,
            version=task.version,
            repo_script_list=[],
            eval_script_list=[],
            env_script_list=[],
            arch='x86_64',
            FAIL_TO_PASS=list(task.fail_to_pass),
            PASS_TO_PASS=list(task.pass_to_pass),
            language=task.language,
            docker_specs={},
            namespace='swebench',
        )
        report = get_eval_report(
            test_spec=spec,
            prediction={
                'instance_id': task.instance_id,
                'model_name_or_path': run_dir.name,
                'model_patch': patch,
            },
            test_log_path=str(log_path),
            include_tests_status=True,
        )[task.instance_id]
    except Exception as exc:
        return EvaluationOutcome(
            error=f'official grader failed: {type(exc).__name__}: {exc}',
            infrastructure_error=True,
            patch_applies=True,
            duration_ms=int((time.time() - started) * 1000),
        )
    status = report.get('tests_status') or {}
    f2p = status.get('FAIL_TO_PASS') or {}
    p2p = status.get('PASS_TO_PASS') or {}
    f2p_success = len(f2p.get('success') or [])
    f2p_total = f2p_success + len(f2p.get('failure') or [])
    p2p_success = len(p2p.get('success') or [])
    p2p_total = p2p_success + len(p2p.get('failure') or [])
    resolved = bool(report.get('resolved')) and f2p_total > 0
    infra = not f2p_total and not p2p_total
    return EvaluationOutcome(
        resolved=resolved,
        patch_applies=bool(report.get('patch_successfully_applied')),
        fail_to_pass_passed=f2p_success,
        fail_to_pass_total=f2p_total,
        pass_to_pass_passed=p2p_success,
        pass_to_pass_total=p2p_total,
        duration_ms=int((time.time() - started) * 1000),
        error=('no tests were graded' if infra else ''),
        infrastructure_error=infra,
    )


def validate_runtime() -> list[str]:
    errors = []
    for path, label in (
        (UDOCKER_BIN, 'udocker'),
        (TOFU_RUNTIME / 'python' / 'bin' / 'python3', 'Tofu Python runtime'),
        (TOFU_SOURCE / 'server.py', 'Tofu source snapshot'),
        (CODEX_PACKAGE / 'bin' / 'codex', 'Codex binary'),
        (CODEX_HOME / 'auth.json', 'Codex authentication'),
    ):
        if not path.exists():
            errors.append(f'{label} missing: {path}')
    runtime_python = TOFU_RUNTIME / 'venv' / 'bin' / 'python'
    if runtime_python.is_file():
        try:
            probe = subprocess.run(
                [str(runtime_python), '-c',
                 'import quart, hypercorn, orjson, requests'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f'Tofu dependency probe crashed: {exc}')
        else:
            if probe.returncode != 0:
                errors.append(
                    'Tofu dependency probe failed: '
                    + (probe.stderr or probe.stdout or '')[-500:])
    return errors


__all__ = [
    'BENCHMARK_ALLOW_HOSTS', 'BENCHMARK_MODEL_ID',
    'BENCHMARK_PROVIDER_ID', 'BENCHMARK_PUBLIC_PRICING',
    'ARM_CONFIGS', 'EvaluationOutcome', 'InferenceOutcome',
    'PUBLIC_GPT56_SOL_PRICING', 'arm_config', 'build_agent_prompt',
    'ensure_container', 'evaluate_patch', 'run_codex', 'run_tofu',
    'validate_runtime',
]
