"""lib/tasks_pkg/handlers/local_serve.py — Managed local deployment tools.

Thin adapter over ``lib.local_serve.api``: every function there already
returns a structured, user-facing dict; this module only formats it for the
model and stamps the tool round. The two human-confirmed boundaries
(``local_serve_deploy`` / ``local_serve_remove``) consume the approval
receipt minted by the confirmation gate — without a receipt the call is a
typed rejection, identical to ``request_skill_install``.
"""

from __future__ import annotations

from lib.local_serve import api as serve_api
from lib.local_serve.tool_defs import LOCAL_SERVE_TOOL_NAMES
from lib.log import get_logger
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
from lib.tasks_pkg.executor import tool_registry
from lib.tool_rejection import stamp_tool_rejection

logger = get_logger(__name__)

_GIB = 1 << 30


def _gib(n) -> str:
    return '%.1f' % (float(n) / _GIB) if isinstance(n, (int, float)) else '?'


def _fmt_hardware(hw: dict) -> str:
    gpus = hw.get('gpus') or []
    if gpus:
        gpu_txt = '；'.join(
            'GPU%d %s（显存 %s/%s GiB 可用）'
            % (g.get('index', '?'), g.get('name', '?'),
               _gib(g.get('vram_free_bytes')), _gib(g.get('vram_total_bytes')))
            for g in gpus)
    else:
        gpu_txt = '未检测到 NVIDIA GPU（将走 CPU/llama.cpp 路线）'
    return ('%s；内存可用 %s GiB；磁盘可用 %s GiB'
            % (gpu_txt, _gib(hw.get('ram_available_bytes')),
               _gib(hw.get('disk_free_bytes'))))


def _fmt_prepare(result: dict) -> str:
    if not result.get('ok'):
        return ('本地部署预检失败（%s 阶段）：%s'
                % (result.get('stage', '?'), result.get('error', '未知原因')))
    plan = result['plan']
    insp = result['inspection']
    lines = [
        '预检完成，推荐方案如下：',
        '模型：%s（%s，%s）' % (
            plan.get('served_name') or insp.get('path'),
            insp.get('format'), insp.get('architecture') or '未知架构'),
        '硬件：%s' % _fmt_hardware(result.get('hardware') or {}),
        '引擎：%s（资源档位 %s）' % (plan.get('engine'), plan.get('tier')),
        '端点：%s' % plan.get('base_url'),
        '启动命令：%s' % ' '.join(plan.get('argv') or []),
    ]
    for note in plan.get('notes') or []:
        lines.append('说明：%s' % note)
    degrade = [d for d in (plan.get('degrade') or []) if d.get('note')]
    if degrade:
        lines.append('OOM 兜底链：%s' % ' → '.join(d['note'] for d in degrade))
    lines.append('用户确认后调用 local_serve_deploy 开始部署（会安装引擎并启动服务）。')
    return '\n'.join(lines)


_STATUS_LABEL = {
    'planned': '已规划', 'installing': '安装引擎中', 'starting': '启动中',
    'running': '运行中', 'stopped': '已停止', 'failed': '失败',
}


def _fmt_instance(row: dict) -> str:
    lines = [
        '实例 %s：%s' % (row.get('id'),
                      _STATUS_LABEL.get(row.get('status'), row.get('status'))),
        '  引擎 %s · 模型 %s' % (row.get('engine'),
                               row.get('served_name') or row.get('model_path')),
    ]
    if row.get('base_url'):
        lines.append('  端点 %s' % row['base_url'])
    if row.get('provider_id'):
        lines.append('  已注册为模型提供商（%s），可在模型选择器中直接选用'
                     % row['provider_id'])
    if row.get('last_error'):
        lines.append('  最近错误：%s' % row['last_error'])
    return '\n'.join(lines)


def _fmt_status(result: dict) -> str:
    if not result.get('ok'):
        return '查询失败：%s' % result.get('error', '未知实例')
    lines = [_fmt_instance(result)]
    if result.get('status') == 'running':
        lines.append('  进程存活：%s；/models 应答：%s'
                     % ('是' if result.get('pid_alive') else '否',
                        '正常' if result.get('serving') else '异常'))
    tail = result.get('log_tail') or ''
    if tail and result.get('status') in ('failed', 'installing', 'starting'):
        lines.append('日志尾部：\n%s' % tail[-1200:])
    return '\n'.join(lines)


def _rejected(round_entry, fn_name, content):
    stamp_tool_rejection(
        round_entry,
        {'kind': 'approval_receipt_missing', 'tool': fn_name},
        reason=content, retryable=False)


@tool_registry.tool_set(
    LOCAL_SERVE_TOOL_NAMES,
    category='local_serve',
    description='Deploy and manage local model servers (vLLM/SGLang/Ollama/llama.cpp)')
def _handle_local_serve_tool(task, tc, fn_name, tc_id, fn_args, rn,
                             round_entry, cfg, project_path, project_enabled,
                             all_tools=None):
    from lib.tasks_pkg.tool_dispatch._approval import consume_approval_receipt
    from lib.tasks_pkg.manager._registry import task_user_id

    status = 'done'
    title = str(fn_args.get('model_path') or fn_args.get('instance_id') or '')
    owner_user_id = task_user_id(task)
    try:
        if fn_name == 'local_serve_prepare':
            engine = fn_args.get('engine') or None
            content = _fmt_prepare(serve_api.prepare(
                str(fn_args.get('model_path') or ''), engine=engine))

        elif fn_name == 'local_serve_deploy':
            if not consume_approval_receipt(task, fn_name, tc_id, fn_args):
                content = ('部署已被拦截：这次调用没有对应的人工批准回执。'
                           '请先向用户展示 local_serve_prepare 的方案并获得同意。')
                status = 'rejected'
                _rejected(round_entry, fn_name, content)
            else:
                created = serve_api.create_deployment(
                    str(fn_args.get('model_path') or ''),
                    owner_user_id=owner_user_id,
                    engine=fn_args.get('engine') or None)
                if not created.get('ok'):
                    content = ('部署创建失败（%s 阶段）：%s'
                               % (created.get('stage', '?'),
                                  created.get('error', '未知原因')))
                    status = 'error'
                else:
                    instance_id = created['instance']['id']
                    kicked = serve_api.start_deployment_async(
                        instance_id, owner_user_id=owner_user_id)
                    if not kicked.get('ok'):
                        content = '部署启动失败：%s' % kicked.get('error')
                        status = 'error'
                    else:
                        content = (
                            '部署已在后台开始。\n'
                            '实例 ID：%s\n'
                            '接下来会依次安装引擎、启动服务、等待就绪'
                            '（OOM 时自动按兜底链降级重试），完成后自动注册为模型提供商。\n'
                            '请用 local_serve_status 轮询该实例，'
                            '直到状态变为 running（可用）或 failed（把日志尾部转述给用户）。'
                            % instance_id)

        elif fn_name == 'local_serve_status':
            content = _fmt_status(serve_api.deployment_status(
                str(fn_args.get('instance_id') or ''),
                owner_user_id=owner_user_id))

        elif fn_name == 'local_serve_list':
            rows = serve_api.list_deployments(
                owner_user_id=owner_user_id).get('instances') or []
            content = ('当前没有已知的本地托管部署。'
                       if not rows else
                       '本地托管部署（%d 个）：\n%s'
                       % (len(rows), '\n\n'.join(_fmt_instance(r) for r in rows)))

        elif fn_name == 'local_serve_stop':
            result = serve_api.stop_deployment(
                str(fn_args.get('instance_id') or ''),
                owner_user_id=owner_user_id)
            if result.get('ok'):
                content = '已停止实例 %s（部署保留，可随时重新启动）。' % (
                    fn_args.get('instance_id') or '')
            else:
                content = '停止失败：%s' % result.get('error', '未知原因')
                status = 'error'

        elif fn_name == 'local_serve_remove':
            if not consume_approval_receipt(task, fn_name, tc_id, fn_args):
                content = '移除已被拦截：这次调用没有对应的人工批准回执。'
                status = 'rejected'
                _rejected(round_entry, fn_name, content)
            else:
                result = serve_api.remove_deployment(
                    str(fn_args.get('instance_id') or ''),
                    owner_user_id=owner_user_id)
                if result.get('ok'):
                    content = ('实例 %s 已移除：服务已停止，模型提供商已注销。'
                               '引擎环境与模型文件保留在磁盘上未删除。'
                               % (fn_args.get('instance_id') or ''))
                else:
                    content = '移除失败：%s' % result.get('error', '未知原因')
                    status = 'error'

        else:
            content = 'Unknown local_serve tool: %s' % fn_name
            status = 'error'
    except Exception as exc:
        logger.warning('[LocalServe] %s failed: %s', fn_name, exc,
                       exc_info=True)
        content = '本地部署操作失败：%s' % exc
        status = 'error'

    ok = status == 'done' and not content.startswith((
        '本地部署预检失败', '查询失败', '停止失败', '移除失败', '部署创建失败',
        '部署启动失败', 'Unknown'))
    meta = _build_simple_meta(
        fn_name, content, source='LocalServe',
        title=title, snippet=content.split('\n', 1)[0][:160],
        badge='🖥️ ready' if ok else '❌ blocked')
    if status != 'done':
        meta['writeOk'] = False
    _finalize_tool_round(task, rn, round_entry, [meta], status=status)
    return tc_id, content, False
