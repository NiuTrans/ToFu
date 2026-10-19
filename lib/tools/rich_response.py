"""Discoverable native rich response tools and scoped image publication hooks."""
import json

from functools import lru_cache
from pathlib import Path

from lib.log import get_logger

logger = get_logger(__name__)

NAMES = frozenset({'publish_response_asset', 'publish_response_file', 'publish_rich_response'})


@lru_cache(maxsize=1)
def _payload_schema_bytes():
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/rich_response_v1.schema.json').read_text())
    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if '$ref' in value:
            return expand(schema['$defs'][value['$ref'].split('/')[-1]])
        return {key: expand(item) for key, item in value.items() if key not in ('$defs', '$id', '$schema')}
    return json.dumps(expand(schema), separators=(',', ':'))


def _payload_schema():
    # Cache immutable bytes, not a mutable request-owned schema or authority.
    return json.loads(_payload_schema_bytes())


def build_tools(ctx):
    binding = (getattr(ctx, 'cfg', {}) or {}).get('evolution') or {}
    if not ctx.durable_state_available or not ctx.owner_user_id or not (ctx.conv_id or binding.get('run_id')):
        return []
    rows = [

        {'type': 'function', 'function': {'name': 'publish_response_file',
         'description': 'Publish an existing document or attachment from verified task project roots into an owned durable artifact. Use after generating HTML/Markdown/SVG/text/PDF/ZIP/office files, including self-contained interactive prototypes. A workspace path is not a web URL. Returns verified byte/hash metadata and browser delivery URLs; reference the artifact ID with native open/download actions. Safe preview blocks document scripts by default; script execution is a separate human choice. Multi-file directories are not mounted or dependency-resolved. Never publish private files without user authorization.',
         'parameters': {'type': 'object', 'properties': {'path': {'type': 'string', 'minLength': 1}, 'title': {'type': 'string', 'maxLength': 300}}, 'required': ['path'], 'additionalProperties': False}}},
        {'type': 'function', 'function': {'name': 'publish_response_asset',
         'description': 'Publish a PNG/JPEG/WebP/GIF from verified task project roots as an owned durable response image. Use explicitly after run_command image generation. Never publish shell-output paths automatically.',
         'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'alt': {'type': 'string', 'maxLength': 1000}}, 'required': ['path', 'alt'], 'additionalProperties': False}}},
        {'type': 'function', 'function': {'name': 'publish_rich_response',
         'description': 'Publish a native interactive workspace: optional stack/grid/tabs layout groups, metric_grid indicators with unknown values and trends, workflow progress and next steps, notice explanations, resource_panel live compute capacity (provider ID from environment_providers/compute_catalog; use connection for an owned MCP server exposing read-only compute_catalog; for get_queue_resource use provider=queue, connection and exact queue), measured comparison/chart, general data_chart (line/scatter/bar/pie), timing diagram (timeline), table, image gallery, supplied-source citations, server-issued choice_prompt or form_prompt (ask_human free_text/batch), evidence, artifact reference or read-only actions. Prefer native blocks over ASCII diagrams or code-only answers when these components fit the explanation. Data is schema-validated and rendered automatically in this tool card and the artifact panel; retain a concise textual answer as fallback. For inline answers you may also emit the same valid JSON in a tofu-ui fenced block. Only owned same-conversation asset/artifact IDs are permitted. Publish local PNG/JPEG/WebP/GIF through publish_response_asset first; never use workspace paths as image URLs. Relative improvement cannot override failed or unknown acceptance. Citation blocks contain supplied HTTP(S) sources; reference them in prose as [[cite:blockId/sourceId]]. Previews never fetch or claim source verification. choice_prompt/form_prompt only references an existing ask_human interactionId; question/options/answer state come from the owned server turn, never this payload. Use ask_human to create a real question, not invented IDs. Resource panels refresh through the owned task/run provider connection without model polling; snapshots are historical evidence. Headless Evolution publication is saved in the existing tool result, with no invented conversation. No model HTML or JavaScript executes.',
         'parameters': {'type': 'object', 'properties': {'payload': _payload_schema()}, 'required': ['payload'], 'additionalProperties': False}}},
    ]
    return rows if ctx.conv_id else [row for row in rows if row['function']['name'] == 'publish_rich_response']



def build_spec():
    """One declaration shared by live assembly and the inventory projection."""
    from lib.tools.registry._spec import ToolSpec
    return ToolSpec(
        'rich_response', build_tools, phase='capability',
        provides=NAMES, write_tools=NAMES, cacheable_tools=frozenset(),

        discovery_policy='searchable',

        timeout_ms_by_name={name: 120_000 for name in NAMES},
        category='artifacts',
        description='Publish owned rich responses, images, documents and attachments',
        gate='Authenticated durable conversation or authorized Evolution producer', handler=handle,
        native_route_groups=frozenset({'download', 'image_gen', 'produce'}),
        search_hints={
            'publish_response_file': 'publish deliver existing local file html markdown document prototype download attachment report 上传 发布 文件 交付 原型 链接 下载 附件',
            'publish_response_asset': 'publish generated chart image screenshot picture 图表 图片 截图 发布',
            'publish_rich_response': 'native rich response dynamic dashboard resources queues metric workflow form chart timeline diagram table gallery artifact buttons 动态 界面 仪表盘 资源 队列 表单 进度 富文本 图表 时序图 表格 原生 卡片 按钮',
        },
    )


def task_roots(task, project_path):
    from lib.tasks_pkg.workspace_delivery import ensure_execution_roots
    from lib.project_mod.config import build_root_policy
    config = task.get('config') or {}
    policy = ensure_execution_roots(task) or task.get('_workspaceRootPolicy')
    if policy is None:
        primary = config.get('projectPath')
        if not primary or not project_path or Path(primary).absolute() != Path(project_path).absolute():
            raise PermissionError('Verified task project roots required')
        policy = build_root_policy(primary, config.get('projectPaths'), config.get('readOnlyPaths'))
    return [state['path'] for state in policy['roots'].values()]


def publish_for_task(task, path, alt, project_path):

    from lib.desktop.remote import remote_worktree_binding
    if remote_worktree_binding(task.get('config') or {}):
        raise PermissionError('Remote worktree image must be explicitly transferred to server staging before publication')
    from lib.response_assets import publish_asset
    from lib.tasks_pkg.manager._registry import task_user_id
    from lib.tasks_pkg.workspace_delivery import route_project_tool_args
    path = route_project_tool_args(task, 'read_files', {'path': path})['path']
    return publish_asset(path, alt, conv_id=task.get('convId'), user_id=task_user_id(task),
                         roots=task_roots(task, project_path), task_id=task.get('id', ''))


def publish_file_for_task(task, path, title, project_path):
    """Explicit publication borrows read authority; it never expands roots."""
    from lib.desktop.remote import remote_worktree_binding
    if remote_worktree_binding(task.get('config') or {}):
        raise PermissionError('Remote worktree file must be explicitly transferred to the server before publication')
    from lib.artifacts.response_files import publish_file
    from lib.tasks_pkg.manager._registry import task_user_id
    from lib.tasks_pkg.workspace_delivery import route_project_tool_args
    path = route_project_tool_args(task, 'read_files', {'path': path})['path']
    abort_event = task.get('abort_event')
    return publish_file(path, title, conv_id=task.get('convId'), user_id=task_user_id(task),
                        roots=task_roots(task, project_path), task_id=task.get('id', ''),
                        cancelled=lambda: bool(task.get('aborted') or (abort_event and abort_event.is_set())))


def image_posthook(task, fn_name, fn_args, project_path, result):
    """Never parse tool/shell output. Only named read/write inputs are candidates."""
    if fn_name not in ('read_files', 'write_file') or not project_path:
        return []
    if isinstance(result, str) and (result.startswith('Error:') or 'refused' in result[:150].lower()):
        return []
    paths = [item.get('path') for item in fn_args.get('reads', [])] if fn_args.get('reads') else [fn_args.get('path')]
    assets = []
    for path in paths[:20]:
        if not isinstance(path, str) or Path(path).suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.gif'):
            continue
        try:
            assets.append(publish_for_task(task, path, Path(path).name, project_path))
        except Exception as diagnostic_error:
            # Supplementary publication never changes an underlying read/write verdict.
            logger.debug('image_posthook failure handled: %s', type(diagnostic_error).__name__)
            continue
    return assets


def handle(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path,
           project_enabled, all_tools=None):
    from lib.tasks_pkg.executor import _finalize_tool_round
    from lib.tasks_pkg.manager._registry import task_user_id
    from lib.response_assets import publish_rich
    from lib.artifacts.core import public_meta
    meta = {'toolName': fn_name, 'source': 'RichResponse', 'url': ''}
    try:
        if fn_name == 'publish_response_asset':
            result = publish_for_task(task, fn_args['path'], fn_args['alt'], project_path)
            meta['responseAssets'] = [result]

        elif fn_name == 'publish_response_file':
            result = publish_file_for_task(task, fn_args['path'], fn_args.get('title', ''), project_path)
            meta.update(artifactId=result['id'], artifactFormat=result['format'], responseFile=result)
            from lib.artifacts.events import emit_artifact_event
            emit_artifact_event(task, result['artifact'])
        else:
            if task.get('convId'):
                artifact = publish_rich(fn_args['payload'], conv_id=task.get('convId'),
                                        user_id=task_user_id(task), task_id=task.get('id', ''))
                receipt = {key: value for key, value in public_meta(artifact).items() if key not in ('content', 'meta')}
                result = {'artifact': receipt, 'richResponse': fn_args['payload']}
                meta.update(artifactId=artifact['id'], artifactFormat='rich', richResponse=fn_args['payload'])

                from lib.artifacts.events import emit_artifact_event
                emit_artifact_event(task, artifact)
            else:
                from lib.evolution.coordination import authorize_task
                from lib.response_assets import validate_rich
                run, role, _ = authorize_task(task)
                if role not in ('executing', 'coordinator'):
                    raise PermissionError('Current Evolution producer required')
                validate_rich(fn_args['payload'], conv_id=None, user_id=task_user_id(task), run_id=run['run_id'])
                result = {'richResponse': fn_args['payload'], 'runId': run['run_id'], 'taskId': task['id']}
                meta.update(richResponse=fn_args['payload'])
        content = json.dumps(result, ensure_ascii=False)
        _finalize_tool_round(task, rn, round_entry, [meta])
    except Exception as exc:
        content = 'Error: Response publication unavailable (' + type(exc).__name__ + '). Check supported file types, owned references, schema v1, and verified project roots.'
        meta['snippet'] = content
        _finalize_tool_round(task, rn, round_entry, [meta], status='error')
    return tc_id, content, False
