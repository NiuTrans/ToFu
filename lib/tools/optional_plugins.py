"""Bundled optional tool plugins, enabled through the ordinary plugin allow-list.

Owns domain schema composition and declarations; register_bundled_plugins is
called by plugin discovery. Domain services retain execution, authorization,
storage and recovery. Imports here never load a capability runtime.
"""
from __future__ import annotations
from dataclasses import replace
from typing import Any
from lib.log import get_logger
from lib.tools.environment_capabilities import BROWSER, NODE
from lib.tools.registry._plugins import resolve_enabled_plugins
from lib.tools.registry._spec import ToolContext, ToolSpec, all_specs
logger = get_logger(__name__)

# Distinctive product/tool vocabulary that names a bundled optional tool in a
# query. Generic capability words stay out on purpose (same rule as the
# connector evidence): the hint fires only when the user actually named a
# dormant tool, not whenever a word in its search hints happens to score.
_BUNDLED_DISCOVERY_ALIASES: dict[str, tuple[str, ...]] = {
    'produce_slides': ('ppt', 'pptx', '幻灯片', '课件', '路演',
                       'slide deck', 'presentation', 'powerpoint', 'keynote'),
    'produce_report': ('produce_report', '生成报告'),
    'produce_research': ('produce_research', '深度研究'),
    'produce_video': ('produce_video', '制作视频', '生成视频', '科普视频'),
    'edit_slides': ('edit_slides', '改ppt', '编辑幻灯片'),
    'patch_slides': ('patch_slides', 'restyle_slides', '换配色', '换字体'),
    'revert_slides': ('revert_slides', 'revert_video', '恢复上一版'),
    'paper_build': ('paper_build', 'paper_layout', 'latex', 'xelatex',
                    'pdflatex', '编译论文', '论文排版'),
    'paper_import_arxiv': ('paper_import_arxiv', 'arxiv', 'eprint'),
    'paper_check': ('paper_check', 'paper_review', 'paper_scaffold',
                    '投稿检查', '审稿', '论文骨架'),
    'paper_cite_search': ('paper_cite_search', 'paper_cite_insert',
                          'paper_citation_audit', '文献检索', '参考文献',
                          'bibliography', '引用核验'),
    'paper_propose_edits': ('paper_propose_edits', '提案修改'),
    'generate_image': ('generate_image', '生成图片', '编辑图片', '配图',
                       '插画', '海报', '封面图'),
    'local_serve_deploy': ('local_serve', 'vllm', 'sglang', 'ollama',
                           'llama.cpp', '本地部署', '本地推理', '本地模型部署'),
}


def bundled_plugin_search_availability(cfg: dict, query: Any, namespace: Any,
                                       usable_tool_names) -> list[dict]:
    """Explain dormant bundled plugins a search names but this task cannot call.

    ``cfg`` is the request config (``config.plugins`` wins over
    ``TOFU_DEFAULT_TOOL_PLUGINS``, exactly as tool assembly resolves it).
    ``usable_tool_names`` is the task's executable catalog, so an enabled
    plugin or an already-callable tool never produces evidence.
    """
    enabled = resolve_enabled_plugins(cfg if isinstance(cfg, dict) else {})
    usable = {str(value).strip().casefold()
              for value in (usable_tool_names or ())}
    named_namespace = str(namespace or '').strip().casefold()
    normalized_query = str(query or '').strip().casefold()
    matched_by_plugin: dict[str, list[str]] = {}
    gate_by_plugin: dict[str, str] = {}
    for spec in all_specs():
        plugin = spec.plugin_name
        if spec.source != 'plugin' or not plugin:
            continue
        # None = allow every plugin; this request exposes it, so no evidence.
        if enabled is None or plugin in enabled:
            continue
        if plugin not in gate_by_plugin:
            gate_by_plugin[plugin] = spec.gate or f'config.plugins: {plugin}'
        for name in sorted(spec.provides):
            folded = name.casefold()
            if folded in usable:
                continue
            aliases = (folded, *_BUNDLED_DISCOVERY_ALIASES.get(name, ()))
            if named_namespace == plugin.casefold() or any(
                    alias and alias in normalized_query for alias in aliases):
                matched_by_plugin.setdefault(plugin, []).append(name)
    evidence = []
    for plugin, matched in matched_by_plugin.items():
        gate = gate_by_plugin[plugin]
        evidence.append({
            'capability': 'bundled_plugin', 'namespace': plugin,
            'plugin': plugin, 'available': False,
            'reason_code': 'bundled_plugin_not_enabled',
            'tool_name': matched[0], 'matched_tools': matched,
            'message': (
                f"{', '.join(matched)} belongs to the bundled optional "
                f"'{plugin}' capability, which is not enabled for this "
                f'deployment. Its tools exist but are excluded from this '
                "task's catalog by the plugin allow-list."),
            'recovery_hint': (
                f'Ask the user to enable it (gate: {gate}). A dedicated '
                f"deployment adds '{plugin}' to TOFU_DEFAULT_TOOL_PLUGINS "
                'and restarts the server; an /api/v1/agent/run caller can '
                f"instead pass config.plugins=['{plugin}'] (or '*') for one "
                'request. Do not repeat this search until the user confirms '
                'the change.'),
        })
    return evidence


def _build_image_gen(ctx: ToolContext) -> list[dict]:
    from lib.tools.image_gen import GENERATE_IMAGE_TOOL
    logger.debug('[Task %s] 🎨 Image generation tool available', ctx.tid)
    return [GENERATE_IMAGE_TOOL]

def _build_motion_video(ctx: ToolContext) -> list[dict]:
    # Motion-video (MG animation) pipeline — gated on a project being
    # attached (the workdir convention lives under the project's .tofu/),
    # same gate as the project tool family.
    if not ctx.durable_state_available or not ctx.project_ready:
        return []
    from lib.tools.motion_video import MOTION_VIDEO_TOOLS
    logger.debug('[Task %s] Motion-video tools enabled (%d)',
                 ctx.tid, len(MOTION_VIDEO_TOOLS))
    return list(MOTION_VIDEO_TOOLS)

def _build_produce(ctx: ToolContext) -> list[dict]:
    if not ctx.durable_state_available:
        return []
    # Production is an explicit optional capability; recipe dependencies
    # such as web grounding remain owned by the production service.
    from lib.tools.produce import (PRODUCE_REPORT_TOOL,
                                   PRODUCE_RESEARCH_TOOL, PRODUCE_SLIDES_TOOL,
                                   PRODUCE_VIDEO_TOOL)
    logger.debug('[Task %s] produce_video/produce_report/produce_research/'
                 'produce_slides tools enabled', ctx.tid)
    # Appended LAST so the existing video/report prefix stays byte-stable for
    # the prompt cache (the ordering contract in this module's docstring).
    return [PRODUCE_VIDEO_TOOL, PRODUCE_REPORT_TOOL, PRODUCE_RESEARCH_TOOL,
            PRODUCE_SLIDES_TOOL]

def _build_produce_edits(ctx: ToolContext) -> list[dict]:
    if not ctx.durable_state_available:
        return []
    # Conversational revision of ALREADY-PRODUCED artifacts. Unlike the
    # produce_* family these never touch research/web grounding, so they must
    # NOT disappear with the Web Search switch (the exposure gate above is an
    # explicit produce-only product decision) — a user who generated a deck
    # yesterday and turns search off today still gets to edit it.
    from lib.tools.produce import (EDIT_SLIDES_TOOL, EDIT_VIDEO_TOOL,
                                   PATCH_SLIDES_TOOL, RESTYLE_SLIDES_TOOL,
                                   REVERT_SLIDES_TOOL, REVERT_VIDEO_TOOL)
    logger.debug('[Task %s] edit/revert tools for produced artifacts '
                 'enabled', ctx.tid)
    return [EDIT_SLIDES_TOOL, EDIT_VIDEO_TOOL, PATCH_SLIDES_TOOL,
            RESTYLE_SLIDES_TOOL, REVERT_SLIDES_TOOL, REVERT_VIDEO_TOOL]

def _build_paper_build(ctx: ToolContext) -> list[dict]:
    # Paper builds need an owned project root (the build service binds sources
    # strictly to conversation roots), so the schema rides the project gate.
    if not ctx.durable_state_available or not ctx.project_ready:
        return []
    from lib.tools.paper_build import (PAPER_ARXIV_IMPORT_TOOL,
                                       PAPER_BUILD_TOOL, PAPER_LAYOUT_TOOL)
    from lib.tools.paper_edits import PAPER_PROPOSE_EDITS_TOOL
    return [PAPER_BUILD_TOOL, PAPER_LAYOUT_TOOL, PAPER_ARXIV_IMPORT_TOOL,
            PAPER_PROPOSE_EDITS_TOOL]

def _build_paper_citations(ctx: ToolContext) -> list[dict]:
    # Citation search/insert/verify binds to the conversation's owned paper
    # document root, so it rides the same project gate as paper_build.
    if not ctx.durable_state_available or not ctx.project_ready:
        return []
    from lib.tools.paper_citations import (PAPER_CITATION_AUDIT_TOOL,
                                           PAPER_CITE_INSERT_TOOL,
                                           PAPER_CITE_SEARCH_TOOL)
    return [PAPER_CITE_SEARCH_TOOL, PAPER_CITE_INSERT_TOOL,
            PAPER_CITATION_AUDIT_TOOL]

def _build_paper_quality(ctx: ToolContext) -> list[dict]:
    # The quality gate reads the same owned live tree and the same retained
    # build artifacts as paper_build, so it rides the same project gate.
    if not ctx.durable_state_available or not ctx.project_ready:
        return []
    from lib.tools.paper_quality import (PAPER_CHECK_TOOL, PAPER_REVIEW_TOOL,
                                         PAPER_SCAFFOLD_TOOL)
    return [PAPER_CHECK_TOOL, PAPER_REVIEW_TOOL, PAPER_SCAFFOLD_TOOL]

def _build_local_serve(ctx: ToolContext) -> list[dict]:
    # Managed local deployment (install + run an inference engine for a
    # user-supplied model path) is a DEFAULT capability like the scheduler:
    # it attaches whenever any base tool exists. It is discoverable through
    # Tool Search rather than eagerly wired, so idle turns pay nothing.
    # TOFU_LOCAL_SERVE=0 is the retraction switch.
    if (not ctx.durable_state_available
            or ctx.lean or not ctx.has_base_tools):
        return []
    import os
    if os.environ.get('TOFU_LOCAL_SERVE', '1').strip().lower() in (
            '0', 'false', 'no', 'off'):
        return []
    from lib.local_serve.tool_defs import LOCAL_SERVE_TOOLS
    logger.debug('[Task %s] 🖥️ Local-serve tools enabled (%d tools)',
                 ctx.tid, len(LOCAL_SERVE_TOOLS))
    return list(LOCAL_SERVE_TOOLS)

def build_optional_specs() -> list[ToolSpec]:
    return [
        ToolSpec('image_gen', _build_image_gen, phase='base',
                 provides=frozenset({'generate_image'}),
                 discovery_policy='searchable',
                 search_hints={
                     'generate_image': (
                         'create edit image picture cover poster illustration '
                         '生成图片 编辑图片 封面图 海报 插画 配图'),
                 },
                 category='image', description='Image generation and editing',
                 gate='设置 → 显示 → 图像生成开关',
                 exposure_gate=lambda ctx: ctx.image_gen_enabled,
                 pin_on_exposure=True),
        ToolSpec('motion_video', _build_motion_video, phase='base',
                 provides=frozenset({
                     'motion_video_env_check', 'motion_video_storyboard_check',
                     'motion_video_check', 'motion_video_render',
                     'motion_video_probe', 'motion_video_concat',
                     'motion_video_narrate', 'motion_video_mux',
                 }),
                 write_tools=frozenset({
                     # Checks may atomically publish boundary contact sheets.
                     'motion_video_check', 'motion_video_render', 'motion_video_concat',
                     'motion_video_narrate', 'motion_video_mux',
                 }),
                 idempotent_tools=frozenset({
                     'motion_video_env_check', 'motion_video_storyboard_check',
                     'motion_video_check', 'motion_video_probe',
                 }),
                 # These inspect mutable project files or host capabilities.
                 cacheable_tools=frozenset(),
                 # HyperFrames is a Node CLI whose check and render boot a
                 # headless Chromium. env_check stays callable to diagnose and
                 # bootstrap the chain.
                 environment_requirements_by_name={
                     'motion_video_check': frozenset({NODE, BROWSER}),
                     'motion_video_render': frozenset({NODE, BROWSER}),
                 },
                 unchanged_receipt_tools=frozenset({
                     'motion_video_env_check', 'motion_video_storyboard_check',
                     'motion_video_probe',
                 }),
                 discovery_policy='searchable',
                 category='video',
                 description='Motion video (MG animation) generation',
                 gate='挂载项目后可用'),
        ToolSpec('produce', _build_produce, phase='base',
                 provides=frozenset({'produce_video', 'produce_report',
                                     'produce_research', 'produce_slides'}),
                 discovery_policy='searchable',
                 search_hints={
                     'produce_video': (
                         'make finished video film clip short video '
                         '制作视频 生成视频 做个视频 科普视频 短视频 宣传片'),
                     'produce_report': 'write complete report 生成报告',
                     'produce_research': 'deep research study 深度研究',
                     'produce_slides': (
                         'deck presentation powerpoint ppt pptx slides keynote '
                         '演示文稿 幻灯片 课件 路演 做PPT'),
                 },
                 category='video',
                 description=(
                     'High-level topic → finished video / editable slides / '
                     'report / research'),
                 gate='搜索开启后可用',
                 exposure_gate=lambda ctx: (
                     ctx.search_mode in ('single', 'multi')
                     or ctx.search_enabled)),
        ToolSpec('produce_edits', _build_produce_edits, phase='base',
                 provides=frozenset({'edit_slides', 'edit_video',
                                     'patch_slides', 'restyle_slides',
                                     'revert_slides', 'revert_video'}),
                 discovery_policy='searchable',
                 search_hints={
                     'edit_slides': 'revise presentation deck 编辑幻灯片 改PPT',
                     'edit_video': 'revise video scene 编辑视频 改镜头',
                     'patch_slides': ('change slide text style color '
                                      '改标题 改字号 换图片'),
                     'restyle_slides': ('change deck theme palette fonts '
                                        '换主题 换配色 换字体'),
                     'revert_slides': ('undo deck edit restore version '
                                       '撤销 回滚 恢复上一版'),
                     'revert_video': ('undo video scene edit restore '
                                      '回滚视频镜头 恢复上一版'),
                 },
                 category='video',
                 description='Conversational revision of produced artifacts',
                 # Edits deliberately never gate on Web Search (a deck made
                 # yesterday stays editable with search off) — always on.
                 gate='常开（默认工具，无开关）'),
        ToolSpec('paper_build', _build_paper_build, phase='base',
                 provides=frozenset({'paper_build', 'paper_layout',
                                     'paper_import_arxiv',
                                     'paper_propose_edits'}),
                 # The import downloads into the workspace (a fresh arxiv-<id>/
                 # directory), so it rides the write partition + Manual
                 # approval like other project mutations.
                 write_tools=frozenset({'paper_import_arxiv'}),
                 cacheable_tools=frozenset(),
                 timeout_ms_by_name={'paper_build': 280_000, 'paper_layout': 30_000,
                                     'paper_import_arxiv': 180_000,
                                     'paper_propose_edits': 60_000},
                 discovery_policy='searchable',
                 search_hints={
                     'paper_build': (
                         'compile latex tex project build pdf paper preview '
                         'xelatex pdflatex bib citation error diagnostics '
                         '编译 论文 排版 参考文献 报错 诊断'),
                     'paper_layout': (
                         'page count limit budget sections layout fullness '
                         'compress shorten pages 页数 页面 压缩 限制 版面 章节'),
                     'paper_import_arxiv': (
                         'arxiv eprint source import download tex project '
                         '论文 源码 导入 下载 预印本'),
                     # paper_propose_edits parks a review proposal only; the
                     # journaled write happens via the user-driven accept
                     # route, so it is neither a write tool nor cacheable.
                     'paper_propose_edits': (
                         'propose suggest edits review accept reject '
                         '提案 建议 修改 审阅'),
                 },
                 category='project',
                 description='Compile conversation LaTeX paper + verified diagnostics',
                 gate='挂载项目（输入框 → 项目）',
                 exposure_gate=lambda ctx: ctx.project_ready),
        ToolSpec('paper_citations', _build_paper_citations, phase='base',
                 provides=frozenset({'paper_cite_search', 'paper_cite_insert',
                                     'paper_citation_audit'}),
                 write_tools=frozenset({'paper_cite_insert'}),
                 cacheable_tools=frozenset(),
                 timeout_ms_by_name={'paper_cite_search': 30_000,
                                     'paper_cite_insert': 60_000,
                                     'paper_citation_audit': 90_000},
                 discovery_policy='searchable',
                 search_hints={
                     'paper_cite_search': (
                         'search literature references citations openalex doi '
                         'real papers bibliography related work 文献 检索 '
                         '参考文献 论文 引用'),
                     'paper_cite_insert': (
                         'insert cite bibtex bib reference doi crossref '
                         'bibliography add citation 引用 插入 参考文献 '
                         '添加 文献'),
                     'paper_citation_audit': (
                         'verify citations audit references hallucinated '
                         'retracted mismatch bib check submission 引用 审计 '
                         '核验 幻觉 撤稿 参考文献 检查'),
                 },
                 category='project',
                 description='Search, insert and verify real literature citations',
                 gate='挂载项目（输入框 → 项目）',
                 exposure_gate=lambda ctx: ctx.project_ready),
        ToolSpec('paper_quality', _build_paper_quality, phase='base',
                 provides=frozenset({'paper_check', 'paper_review',
                                     'paper_scaffold'}),
                 write_tools=frozenset(),
                 cacheable_tools=frozenset(),
                 timeout_ms_by_name={'paper_check': 60_000,
                                     'paper_review': 60_000,
                                     'paper_scaffold': 15_000},
                 discovery_policy='searchable',
                 search_hints={
                     'paper_check': (
                         'submission readiness quality gate lint latex check '
                         'fonts embedded image dpi references labels '
                         '投稿 检查 就绪 质量 门禁 字体 引用'),
                     'paper_review': (
                         'review critique peer review rubric claims audit '
                         'revision plan 审稿 评审 意见 修改 建议'),
                     'paper_scaffold': (
                         'new paper skeleton scaffold sections starter '
                         'template 新论文 骨架 模板 章节 提纲'),
                 },
                 category='project',
                 description='Paper submission-readiness gate + review dossier',
                 gate='挂载项目（输入框 → 项目）',
                 exposure_gate=lambda ctx: ctx.project_ready),
        ToolSpec('local_serve', _build_local_serve, phase='capability',
                 provides=frozenset({
                     'local_serve_recommend', 'local_serve_prepare',
                     'local_serve_deploy', 'local_serve_status',
                     'local_serve_list', 'local_serve_stop',
                     'local_serve_remove',
                 }),
                 # deploy spawns a background install+server; remove destroys
                 # the registration — both ALWAYS need a human click (receipt
                 # enforced again in the handler). stop kills a running server
                 # but is trivially reversible (deploy starts it again), so it
                 # is an ordinary write gated only in Manual mode.
                 write_tools=frozenset({
                     'local_serve_deploy', 'local_serve_stop',
                     'local_serve_remove',
                 }),
                 confirmation_tools=frozenset({
                     'local_serve_deploy', 'local_serve_remove',
                 }),
                 idempotent_tools=frozenset({
                     'local_serve_recommend', 'local_serve_prepare',
                     'local_serve_list',
                 }),
                 # status reflects a live server; prepare re-probes hardware.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({'local_serve_list'}),
                 programmatic_tools=frozenset({'local_serve_list'}),
                 discovery_policy='searchable',
                 search_hints={
                     'local_serve_recommend': (
                         'recommend open source local model for hardware gpu '
                         'vram choose 推荐 本地模型 开源 硬件 显卡 选择'),
                     'local_serve_prepare': (
                         'inspect local model path hardware plan deploy '
                         '本地模型 路径 检查 部署方案'),
                     'local_serve_deploy': (
                         'install engine start local model server vllm sglang '
                         'ollama llamacpp 部署 启动 本地模型 安装'),
                     'local_serve_status': (
                         'check deployment progress log 部署进度 状态 日志'),
                     'local_serve_list': 'list local deployments 本地部署 列表',
                     'local_serve_stop': (
                         'stop local model server 停止 本地服务'),
                     'local_serve_remove': (
                         'remove unregister local deployment 移除 注销 本地部署'),
                 },
                 category='local_serve',
                 description='Managed local model deployment '
                             '(vLLM/SGLang/Ollama/llama.cpp)',
                 gate='常开（有任意基础工具即挂载；TOFU_LOCAL_SERVE=0 摘除）'),
    ]


def register_bundled_plugins(register):
    """Use the same stamped ToolSpec registry and per-request plugin gate."""
    owners = {'image_gen': 'image_generation', 'motion_video': 'production', 'produce': 'production', 'produce_edits': 'production', 'paper_build': 'papers', 'paper_citations': 'papers', 'paper_quality': 'papers', 'local_serve': 'local_models'}
    for spec in build_optional_specs():
        # Enabling a domain plugin does not pin its whole catalog directly.
        changes = dict(source='plugin', plugin_name=owners[spec.key], host_owned=True)
        if spec.key in {'produce', 'produce_edits'}:
            changes.update(exposure_gate=None, gate='config.plugins: production')
        elif spec.key == 'image_gen':
            changes.update(gate='图片生成开关或 config.plugins: image_generation',
                           exposure_gate=None, pin_on_exposure=False)
        else:
            changes.update(gate='config.plugins: ' + owners[spec.key])
        register(replace(spec, **changes))
