"""Built-in tool specifications, grouped by family.

Extracted from ``_build.py`` so additions land in a focused family
section instead of one shared mega-list. Order is canonical and
append-only: every established tool keeps its prompt-cache position,
and ``custom`` is always last (its handlers are task-local).
"""
from __future__ import annotations

from ._spec import ToolSpec


def build_builtin_specs() -> list:
    """Return the built-in specs in canonical (cache-stable) order."""
    from . import _build as _b  # fully loaded by registration time
    from lib.tools.tool_result_artifacts import TOOL_RESULT_ARTIFACT_CONTRACTS
    from lib.tools.gateway import GATEWAY_TOOL_NAMES
    specs = [
        # ── base phase (counted toward has_real_tools) ──
        ToolSpec('search', _b._build_search, phase='base',
                 provides=frozenset({'web_search'}),
                 idempotent_tools=frozenset({'web_search'}),
                 timeout_ms_by_name={'web_search': 60_000},
                 category='search', description='Web search',
                 gate='输入框 → 搜索模式（联网/multi）',
                 exposure_gate=lambda ctx: ctx.search_mode in (
                     'single', 'multi')),
        ToolSpec('fetch', _b._build_fetch, phase='base',
                 provides=frozenset({'fetch_url'}),
                 idempotent_tools=frozenset({'fetch_url'}),
                 programmatic_tools=frozenset({'fetch_url'}),
                 timeout_ms_by_name={'fetch_url': 60_000},
                 script_safe=True,
                 category='search', description='Fetch a URL',
                 gate='搜索开启或抓取开关（默认开）',
                 exposure_gate=lambda ctx: (
                     ctx.fetch_enabled or ctx.search_enabled)),
        ToolSpec('read_files', _b._build_read_files, phase='base',
                 provides=frozenset({'read_files'}),
                 idempotent_tools=frozenset({'read_files'}),
                 programmatic_tools=frozenset({'read_files'}),
                 result_recovery_by_name={'read_files': 'source'},
                 script_safe=True,
                 category='project', description='Read local files',
                 gate='常开（无需项目）'),
        ToolSpec('inspect_image', _b._build_inspect_image, phase='base',
                 provides=frozenset({'inspect_image'}),
                 idempotent_tools=frozenset({'inspect_image'}),
                 category='project', description='Zoom/rotate/crop image viewer',
                 gate='常开（无需项目）'),
        ToolSpec('project', _b._build_project_or_code_exec, phase='base',
                 provides=frozenset({
                     'grep_search', 'find_files',
                     'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'run_command',
                     'check_background_command', 'kill_background_command',
                     'repo_map',
                 }),
                 write_tools=frozenset({
                     'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'run_command', 'kill_background_command',
                 }),
                 idempotent_tools=frozenset({
                     'grep_search', 'find_files',
                     'check_background_command', 'repo_map',
                 }),
                 programmatic_tools=frozenset({
                     'grep_search', 'find_files',
                 }),
                 search_hints={
                     'grep_search': (
                         'symbol references usages occurrences who calls '
                         'code content search 查找引用 谁调用了'),
                     'find_files': (
                         'locate filenames glob config files 查找文件 配置文件'),
                     'write_file': 'create overwrite save file 写入 保存文件',
                     'edit_file': (
                         'edit insert before after replace patch source code '
                         '修改代码 插入内容 替换'),
                     'apply_diff': (
                         'edit change modify fix implementation source code '
                         '修改代码 修复实现'),
                     'apply_diffs': 'edit modify fix several files 批量修改代码',
                     'insert_content': 'insert append text into file 插入内容',
                     'insert_contents': 'insert append several files 批量插入',
                     'run_command': 'shell terminal execute command 运行命令',
                     'check_background_command': (
                         'check background command output status 后台命令状态'),
                     'kill_background_command': (
                         'kill stop background command process 停止后台进程'),
                     'repo_map': 'repository map symbols outline 代码地图 符号大纲',
                 },
                 category='project', description='Project file tools / code exec',
                 gate='挂载项目（输入框 → 项目）或开启代码执行',
                 exposure_gate=lambda ctx: (
                     ctx.project_ready or ctx.code_exec_enabled),
                 result_meta_builder=_b._build_project_result_meta),
        ToolSpec('browser', _b._build_browser, phase='base',
                 # 15 names = BROWSER_TOOLS (13) + ADVANCED_BROWSER_TOOLS (2).
                 # v2 (): the ten retired names are NOT
                 # declared — their tool_registry registration shrank with
                 # BROWSER_TOOL_NAMES, so there is no handler left to declare
                 # (their display formatters remain for history rendering).
                 provides=frozenset({
                     'browser_navigate', 'browser_read_page', 'browser_list_tabs',
                     'browser_research_page', 'browser_devtools', 'browser_site',
                     'browser_close_tab', 'browser_click', 'browser_type',
                     'browser_press_key', 'browser_execute_js',
                     'browser_screenshot', 'browser_get_cookies',
                     'browser_get_history', 'browser_fill_form',
                     'browser_menu_click',
                     'browser_check', 'browser_select_option',
                     'browser_scroll',
                 }),
                 # search_hints: private retrieval aliases/intents (ranking
                 # weight ×3, never serialized into the wire schema). The
                 # browser family previously had NONE, so discoverability
                 # rode entirely on the terse wire descriptions — and BM25
                 # mis-ranked core intents ("click a button" → get_cookies,
                 # "read what is on the page" → list_tabs, "go to a url" →
                 # list_tabs). These hints pin each action verb to its tool.
                 search_hints={
                     'browser_click': (
                         'click press button link tap element click on '
                         'single one 点击 按钮 链接 点击元素 单击'),
                     'browser_navigate': (
                         'navigate go to open website url visit load page '
                         '打开网址 访问 跳转 进入网站'),
                     'browser_read_page': (
                         'read see view page content text what is on extract '
                         '看页面 读取内容 页面文字 提取'),
                     'browser_type': (
                         'type enter input text field write 输入 填写 打字'),
                     'browser_fill_form': (
                         'fill form multiple fields several at once upload '
                         'file attach batch 填表 表单 上传文件 批量填写 多个字段'),
                     'browser_press_key': (
                         'press key enter escape tab keyboard shortcut hotkey '
                         '按键 回车 快捷键'),
                     'browser_check': (
                         'check uncheck checkbox radio toggle switch '
                         '勾选 复选框 单选 开关'),
                     'browser_select_option': (
                         'select option dropdown combobox pick choose '
                         '下拉选择 选项 下拉框'),
                     'browser_menu_click': (
                         'menu right click context hover dropdown submenu '
                         '菜单 右键 悬停 子菜单'),
                     'browser_screenshot': (
                         'screenshot capture image picture snap 截图 截屏'),
                     'browser_list_tabs': (
                         'list tabs open tabs current tab 标签页 列出标签'),
                     'browser_close_tab': 'close tab 关闭标签页',
                     'browser_execute_js': (
                         'run javascript execute js code evaluate script '
                         '执行js 运行脚本'),
                     'browser_scroll': (
                         'scroll down up top bottom page reveal more content '
                         '滚动 下拉 翻页 到底部 查看更多'),
                 },
                 # These DRIVE the user's real browser session, so they belong
                 # in the serial write partition + behind the Manual approval
                 # gate (_pipeline.py derives needs_approval from it). Until
                 # this was declared, browser_execute_js could run arbitrary JS
                 # in the user's page with no prompt, from the parallel pool.
                 # NOTE: this makes them SERIAL — a deliberate behaviour change;
                 # concurrent clicks on one page were never actually safe.
                 write_tools=frozenset({
                     'browser_navigate', 'browser_click', 'browser_type',
                     'browser_press_key', 'browser_execute_js',
                     'browser_research_page', 'browser_devtools', 'browser_site',
                     'browser_fill_form', 'browser_menu_click',
                     'browser_close_tab',
                     'browser_check', 'browser_select_option',
                 }),
                 # Read-only observers stay parallel-safe AND cacheable within
                 # a task. browser_read_page/screenshot are deliberately NOT
                 # idempotent — the page changes under us between calls.
                 idempotent_tools=frozenset({
                     'browser_list_tabs',
                 }),
                 # Read-only/idempotent does not mean snapshot-stable. Tabs,
                 # page DOM, history and cookies may change between identical
                 # calls, so this live family opts out of same-task reuse.
                 cacheable_tools=frozenset(),
                 discovery_policy='searchable',
                 category='browser', description='Browser automation tools',
                 gate='安装并连接浏览器扩展（设置 → 网络）',
                 exposure_gate=lambda ctx: ctx.browser_enabled,
                 pin_on_exposure=True),
        ToolSpec('desktop', _b._build_desktop, phase='base',
                 # provides = LLM 可见的 10 个(desktop_move_file 刻意不
                 # 暴露,见 lib/desktop_tools.py;它仍列在 write_tools 里)。
                 provides=frozenset({
                     'desktop_list_files', 'desktop_read_file',
                     'desktop_write_file',
                     'desktop_open_file', 'desktop_open_app',
                     'desktop_run_command', 'desktop_screenshot',
                     'desktop_gui_action', 'desktop_clipboard',
                     'desktop_system_info',
                 }),
                 # 约束③:desktop 写/执行工具进串行写分区 + Manual 批准门 ——
                 # 此前未声明,既进并行派发池(竞态)又绕过批准门。
                 # desktop_system_info 豁免(其 kill 分支由 agent 侧参数级
                 # exec 门把守);GUI/screenshot 走 allow_gui 层,不进写分区。
                 write_tools=frozenset({
                     'desktop_write_file', 'desktop_move_file',
                     'desktop_run_command', 'desktop_open_app',
                     'desktop_open_file',
                 }),
                 search_hints={
                     'desktop_screenshot': (
                         'capture display monitor current screen desktop '
                         '截屏 屏幕 桌面画面'),
                     'desktop_clipboard': 'copy paste clipboard 剪贴板 复制 粘贴',
                     'desktop_list_files': 'list computer files 查看电脑文件',
                     'desktop_read_file': 'read computer file 读取电脑文件',
                     'desktop_write_file': 'write save computer file 保存电脑文件',
                     'desktop_run_command': 'run local command shell 执行本地命令',
                     'desktop_open_app': 'launch application 打开应用',
                     'desktop_open_file': 'open local file 打开文件',
                     'desktop_gui_action': 'mouse keyboard click type 鼠标 键盘',
                     'desktop_system_info': 'computer system information 系统信息',
                 },
                 discovery_policy='searchable',
                 category='desktop', description='Desktop agent tools',
                 gate='连接桌面 agent（设置 → 设备）',
                 exposure_gate=lambda ctx: ctx.desktop_enabled,
                 pin_on_exposure=True),
        ToolSpec('image_gen', _b._build_image_gen, phase='base',
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
        ToolSpec('motion_video', _b._build_motion_video, phase='base',
                 provides=frozenset({
                     'motion_video_env_check', 'motion_video_storyboard_check',
                     'motion_video_check', 'motion_video_render',
                     'motion_video_probe', 'motion_video_concat',
                     'motion_video_narrate', 'motion_video_mux',
                 }),
                 write_tools=frozenset({
                     'motion_video_render', 'motion_video_concat',
                     'motion_video_narrate', 'motion_video_mux',
                 }),
                 idempotent_tools=frozenset({
                     'motion_video_env_check', 'motion_video_storyboard_check',
                     'motion_video_check', 'motion_video_probe',
                 }),
                 # These inspect mutable project files or host capabilities.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({
                     'motion_video_env_check', 'motion_video_storyboard_check',
                     'motion_video_check', 'motion_video_probe',
                 }),
                 discovery_policy='searchable',
                 category='video',
                 description='Motion video (MG animation) generation',
                 gate='挂载项目后可用'),
        ToolSpec('produce', _b._build_produce, phase='base',
                 provides=frozenset({'produce_video', 'produce_report',
                                     'produce_research', 'produce_slides',
                                     'edit_slides'}),
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
                     'edit_slides': 'revise presentation deck 编辑幻灯片',
                 },
                 category='video',
                 description=(
                     'High-level topic → finished video / editable slides / '
                     'report / research'),
                 gate='搜索开启后可用',
                 exposure_gate=lambda ctx: (
                     ctx.search_mode in ('single', 'multi')
                     or ctx.search_enabled)),
        # End of base phase: appending HERE keeps every earlier prefix
        # byte-stable for the prompt cache (the produce note above).
        ToolSpec('page_preview', _b._build_page_preview, phase='base',
                 provides=frozenset({'browser_preview_page'}),
                 discovery_policy='searchable',
                 search_hints={
                     'browser_preview_page': (
                         'render preview test html webpage frontend headless '
                         'chromium screenshot 真实浏览器 渲染网页 页面预览 '
                         '看看效果 前端界面'),
                 },
                 category='browser',
                 description='Server-side rendered page preview',
                 gate='挂载项目后可用'),
        ToolSpec('conv_ref', _b._build_conv_ref, phase='base',
                 provides=frozenset({'list_conversations', 'get_conversation'}),
                 write_tools=frozenset(),
                 idempotent_tools=frozenset({'list_conversations',
                                             'get_conversation'}),
                 # Sibling turns can advance while this long task is running.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({
                     'list_conversations', 'get_conversation',
                 }),
                 programmatic_tools=frozenset({
                     'list_conversations', 'get_conversation',
                 }),
                 # Eager: the per-turn sibling-conversation digest names these
                 # two by function name when they are registered — deferring
                 # them re-creates the phantom-tool gap the project-mode
                 # branch was added to close.
                 category='conversation', description='Conversation reference tools',
                 gate='项目模式 或 @ 提及一个会话'),
        # Project Brain contributes zero schemas.  These two tools belong to
        # the isolated execution pipeline and are bound to the automatic work
        # ID; they do not read or mutate Board/Feed/Charter state.
        ToolSpec('project_integration', _b._build_project_integration, phase='base',
                 provides=frozenset({
                     'integration_checkpoint', 'integration_submit',
                     'send_project_message', 'get_project_messages', 'consider_project_message',
                 }),
                 write_tools=frozenset({'send_project_message', 'consider_project_message'}),
                 discovery_policy='searchable',
                 search_hints={
                     'integration_checkpoint': (
                         'snapshot isolated worktree progress milestone '
                         '保存隔离工作区检查点'),
                     'integration_submit': (
                         'submit isolated work for human review merge queue '
                         '提交隔离任务人工审查'),
                 },
                 category='conversation',
                 description='Isolated project integration execution controls',
                 gate='项目模式'),
        ToolSpec('human_guidance', _b._build_human_guidance, phase='base',
                 provides=frozenset({'ask_human'}),
                 category='human', description='Ask the human for guidance',
                 gate='输入框 → 人类指导开关',
                 exposure_gate=lambda ctx: ctx.human_guidance_enabled),
        # update_search_settings — appended at the END of the base phase so
        # every earlier tool's position stays byte-stable for the prompt
        # cache (the "appending HERE" rule this module's header documents).
        # It mutates server-GLOBAL config every conversation feels, so it is
        # approval-gated + serial like the other state-changing tools.
        ToolSpec('search_settings', _b._build_search_settings, phase='base',
                 provides=frozenset({'update_search_settings'}),
                 write_tools=frozenset({'update_search_settings'}),
                 discovery_policy='searchable',
                 category='search', description='Search/fetch pipeline settings',
                 gate='输入框 → 搜索模式（联网/multi）',
                 exposure_gate=lambda ctx: ctx.search_mode in (
                     'single', 'multi')),
        # Local knowledge is appended at the END of the base phase so existing
        # tool positions remain prompt-cache-stable when it is available.
        ToolSpec('knowledge', _b._build_knowledge, phase='base',
                 provides=frozenset({'search_knowledge'}),
                 idempotent_tools=frozenset({'search_knowledge'}),
                 programmatic_tools=frozenset({'search_knowledge'}),
                 discovery_policy='searchable', script_safe=True,
                 category='knowledge',
                 description='Enabled local knowledge base',
                 gate='本地知识库存在且已开启',
                 handler=_b._handle_knowledge,
                 catalog_active_only=True),
        # Explicit server-file semantics are append-only so every established
        # hot tool keeps its prompt-cache position. It rides the same runtime
        # availability gate as fetch_url but has its own stable contract/name.
        ToolSpec('browser_download', _b._build_browser_download, phase='base',
                 provides=frozenset({'browser_download_url_to_server'}),
                 cacheable_tools=frozenset(),
                 search_hints={
                     'browser_download_url_to_server': (
                         'download_url_to_server download save copy export fetch '
                         'archive zip install unzip latest file to server project '
                         'logged-in browser link button cookies intranet SSO '
                         '下载 保存 拷贝 复制 导出 安装 解压 最新版 服务器 本地 '
                         '内网 登录 文件 压缩包 链接 按钮'
                     ),
                 },
                 discovery_policy='eager',
                 native_route_groups=frozenset({
                     'search', 'fetch', 'browser', 'download'}),
                 category='browser',
                 description='Download a URL or page link to server staging',
                 gate='抓取开关（默认开）或连接浏览器扩展',
                 exposure_gate=lambda ctx: (
                     ctx.fetch_enabled or ctx.search_enabled
                     or ctx.search_mode in ('single', 'multi')
                     or ctx.browser_enabled)),
        # paper_build is appended at the END of the base phase so every
        # earlier tool keeps its prompt-cache position (the ordering rule in
        # this module's header). It is NOT a write tool: compilation only
        # produces bounded cache artifacts under the build service's
        # supervision, identical in kind to produce_* rendering, and
        # approval-gating it would break the edit→compile→fix loop that is
        # its entire purpose. Results change with source files, so it opts
        # out of same-task result reuse.
        ToolSpec('paper_build', _b._build_paper_build, phase='base',
                 provides=frozenset({'paper_build', 'paper_layout',
                                     'paper_import_arxiv',
                                     'paper_propose_edits'}),
                 # The import downloads into the workspace (a fresh arxiv-<id>/
                 # directory), so it rides the serial write partition + Manual
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
                     'paper_propose_edits': (
                         'propose suggest edits review accept reject '
                         '提案 建议 修改 审阅'),
                 },
                 category='project',
                 description='Compile conversation LaTeX paper + verified diagnostics',
                 gate='挂载项目（输入框 → 项目）',
                 exposure_gate=lambda ctx: ctx.project_ready),
        # Track C: PID-anchored citations (mirror of _build.py).
        ToolSpec('paper_citations', _b._build_paper_citations, phase='base',
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
        # paper_quality follows paper_build at the END of the base phase
        # (same ordering rule as _build.py). Read-only: no write partition,
        # no approval gate, no same-task result reuse.
        ToolSpec('paper_quality', _b._build_paper_quality, phase='base',
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
        # ── capability phase ──
        ToolSpec('memory', _b._build_memory, phase='capability',
                 provides=frozenset({
                     'search_memories', 'create_memory', 'update_memory',
                     'delete_memory', 'merge_memories',
                 }),
                 write_tools=frozenset({
                     'create_memory', 'update_memory',
                     'delete_memory', 'merge_memories',
                 }),
                 idempotent_tools=frozenset({'search_memories'}),
                 # Memory CRUD can change the same query inside one task.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({'search_memories'}),
                 programmatic_tools=frozenset({'search_memories'}),
                 search_hints={
                     'search_memories': (
                         'recall remember previous decision past conversation '
                         '回忆 找回之前决定 拍板'),
                     'create_memory': 'remember save durable fact 记住 保存记忆',
                     'update_memory': 'correct revise saved memory 修改记忆',
                     'delete_memory': 'forget remove saved memory 忘记 删除记忆',
                     'merge_memories': 'combine duplicate memories 合并记忆',
                 },
                 category='memory', description='Memory CRUD tools',
                 gate='Memory 开关开启且有任意基础工具'),
        ToolSpec('skills', _b._build_skills, phase='capability',
                 provides=frozenset({
                     'search_skills', 'load_skill', 'read_skill_resource',
                 }),
                 idempotent_tools=frozenset({
                     'search_skills', 'load_skill', 'read_skill_resource',
                 }),
                 search_hints={
                     'search_skills': (
                         'find install discover workflow skill 技能 查找 安装'),
                     'load_skill': 'load workflow instructions 加载技能说明',
                     'read_skill_resource': (
                         'read skill reference script resource 技能资源'),
                 },
                 category='skills',
                 description='Bounded skill discovery and disclosure',
                 gate='有任意基础工具'),
        ToolSpec('skill_install', _b._build_skill_install, phase='capability',
                 provides=frozenset({'request_skill_install'}),
                 write_tools=frozenset({'request_skill_install'}),
                 confirmation_tools=frozenset({'request_skill_install'}),
                 auto_approval_tools=frozenset({'request_skill_install'}),
                 search_hints={
                     'request_skill_install': (
                         'install verified catalog skill 安装技能'),
                 },
                 discovery_policy='searchable',
                 category='skills',
                 description='Verified skill catalog installation',
                 gate='有任意基础工具；Auto 自动安装已验证包，Manual 真人确认'),
        ToolSpec('todo', _b._build_todo, phase='capability',
                 provides=frozenset({'todo_write'}),
                 category='task', description='Structured task checklist',
                 gate='常开（有任意基础工具即挂载）'),
        ToolSpec('local_serve', _b._build_local_serve, phase='capability',
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
        ToolSpec('scheduler', _b._build_scheduler, phase='capability',
                 provides=frozenset({
                     'schedule_create', 'schedule_list', 'schedule_manage',
                     'timer_manage', 'await_task',
                 }),
                 # These persist state that OUTLIVES the turn (cron jobs,
                 # polling watchers) and can execute shell/python on a
                 # schedule — approval-eligible + serial. schedule_list /
                 # await_task are pure reads and stay parallel.
                 write_tools=frozenset({
                     'schedule_create', 'schedule_manage',
                     'timer_manage',
                 }),
                 idempotent_tools=frozenset({'schedule_list'}),
                 # Create/manage calls and the scheduler worker mutate this.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({'schedule_list'}),
                 programmatic_tools=frozenset({'schedule_list'}),
                 discovery_policy='searchable',
                 search_hints={
                     'schedule_create': (
                         'create recurring reminder cron once: calendar clock '
                         'scheduled agent countdown daily weekly '
                         '创建定时任务 闹钟 循环 提醒 倒计时 计时器'),
                     'schedule_list': 'show scheduled jobs reminders 查看定时任务',
                     'schedule_manage': (
                         'cancel stop disable remove scheduled job reminder '
                         '取消提醒 停止定时任务'),
                     'timer_create': (
                         'wait until ready poll watcher condition satisfied '
                         'CI build download file exists '
                         '创建条件监视 条件满足 等到就绪 轮询 监视'),
                     'timer_manage': 'cancel stop timer 取消计时器',
                     'await_task': 'wait poll background task 等待后台任务',
                 },
                 category='scheduler', description='Scheduler / proactive agent tools',
                 gate='常开（有任意基础工具即挂载）'),
        ToolSpec('timer_watcher', _b._build_timer_watcher, phase='capability',
                 provides=frozenset({'timer_create'}),
                 direct_wire_tools=frozenset({'timer_create'}),
                 write_tools=frozenset({'timer_create'}),
                 cacheable_tools=frozenset(), discovery_policy='eager',
                 category='scheduler', description='Durable readiness continuation',
                 gate='常开（有基础工具和持久存储即挂载）'),
        ToolSpec(
            'tool_result_artifacts', _b._build_tool_result_artifacts,
            phase='capability',
            provides=frozenset({'read_tool_artifact', 'search_tool_artifact'}),
            idempotent_tools=frozenset({
                'read_tool_artifact', 'search_tool_artifact'}),
            programmatic_tools=frozenset({
                'read_tool_artifact', 'search_tool_artifact'}),
            # An L0 envelope can instruct the model to continue only if the
            # continuation function is already callable on that same wire
            # turn. Keeping it behind Tool Search created a dead-end pointer:
            # models repeatedly reissued the source read because the recovery
            # tool named by the result was absent from their visible surface.
            discovery_policy='eager',
            script_safe=True,
            catalog_active_only=True,
            search_hints={
                'read_tool_artifact': 'continue large result range cursor 继续读取',
                'search_tool_artifact': 'search prior tool result 搜索工具结果',
            },
            category='artifacts',
            description='Bounded continuation for large tool results',
            gate='always on (durable state available)',
            contracts=TOOL_RESULT_ARTIFACT_CONTRACTS,
        ),
        ToolSpec('swarm', _b._build_swarm, phase='capability',
                 # provides lists every name this family has a handler for on
                 # the MAIN dispatch registry (@tool_registry.tool_set over
                 # SWARM_TOOL_NAMES), which is wider than what build() puts in
                 # the master schema:
                 #   * spawn_agents/get_agent_result — in the master schema
                 #   * await_agents — legacy dispatch only; poll owns waiting
                 #   * store/read/list_artifact(s)  — NOT in the master schema;
                 #     they are injected into SUB-AGENTS only
                 #     (SubAgent._inject_artifact_tools) and executed inside the
                 #     sub-agent's own loop. Declared anyway because the handler
                 #     IS reachable on the main registry, so an undeclared name
                 #     would be invisible to the partition tables and to the
                 #     custom-tool collision check in lib/tools/tool_env.py.
                 provides=frozenset({
                     'spawn_agents', 'await_agents', 'get_agent_result',
                     'send_agent_message', 'get_agent_messages',
                     'store_artifact', 'read_artifact', 'list_artifacts',
                     'set_approval_delegate',
                 }),
                 write_tools=frozenset({'send_agent_message'}),
                 search_hints={
                     'send_agent_message': 'send message running child subagent steer context 给子agent发消息 补充指令',
                     'get_agent_messages': 'child message input receipt delivery confirmation 子agent 消息 回执',
                 },
                 # store_artifact is deliberately NOT in write_tools. It writes
                 # to an in-process, per-run ArtifactStore (thread-safe under
                 # its own lock, TTL-expiring, lost on process exit) — it
                 # touches no filesystem, no network, and nothing that outlives
                 # the run. Approval-prompting it would be pure noise, and the
                 # serial-dispatch half of the partition buys nothing over the
                 # store's own lock. This is a deliberate departure from the
                 # "every state-changing tool is partitioned" rule, recorded
                 # here so it reads as a decision rather than an omission.
                 idempotent_tools=frozenset({'list_artifacts'}),
                 # Sub-agents can publish artifacts between two list calls.
                 cacheable_tools=frozenset(),
                 unchanged_receipt_tools=frozenset({
                     'get_agent_result', 'list_artifacts',
                 }),
                 category='swarm', description='Async multi-agent swarm',
                 gate='常开（默认工具，无开关）'),
        ToolSpec('mcp', _b._build_mcp, phase='capability',
                 provides=frozenset({
                     'search_mcp_tools', 'call_mcp_read_tool',
                     'call_mcp_write_tool',
                 }),
                 write_tools=frozenset({'call_mcp_write_tool'}),
                 idempotent_tools=frozenset({'search_mcp_tools'}),
                 discovery_policy='searchable',
                 category='mcp', description='External MCP-server tools',
                 gate='设置 → MCP（默认开，需已连接服务器）'),
        # The discovery schema is injected at the final wire boundary
        # according to the resolved provider/search strategy, not during
        # ordinary registry assembly. Declaring it here keeps ownership,
        # idempotency and custom-tool collision checks authoritative.
        # ``execute_tools`` is declared for ownership/collision/admission and
        # is injected only alongside local Tool Search at the wire boundary;
        # nested calls remain bounded by the executable task catalog.
        ToolSpec('tool_gateway', lambda _ctx: [], phase='capability',
                 provides=GATEWAY_TOOL_NAMES,
                 idempotent_tools=frozenset({'search_tools'}),
                 category='tools',
                 description='Local tool discovery and stable execution gateway',
                 gate='工具搜索启用时出现'),
        # ── self-evolution teach channel (evolution-bound tasks only) ──
        # The exposure gate is the isolation contract: ordinary chat and
        # every internal application never see this tool; the handler
        # re-checks the binding because exposure is prompt policy, not
        # authority.
        ToolSpec('evolution', _b._build_evolution_teach, phase='capability',
                 provides=frozenset({'evolution_teach', 'evolution_material', 'evolution_coordinate', 'evolution_reindex', 'evolution_explore'}),
                 write_tools=frozenset({'evolution_teach', 'evolution_material', 'evolution_coordinate', 'evolution_reindex', 'evolution_explore'}),
                 category='evolution',
                 description='Self-evolution teach channel',
                 gate='任务 config 携带 evolution 绑定时出现',
                 exposure_gate=lambda ctx: bool(
                     (ctx.cfg.get('evolution') or {}).get('goal_id')),
                 handler=_b._handle_evolution_teach),
        # ── unified async handles: append-only position before custom ──
        ToolSpec('background', _b._build_background, phase='capability',
                 provides=frozenset({'poll', 'kill'}),
                 write_tools=frozenset({'kill'}),
                 idempotent_tools=frozenset({'poll'}),
                 # Background state changes constantly; results are never
                 # cacheable across rounds.
                 cacheable_tools=frozenset(),
                 # Lifecycle control is never a discovery-only capability.
                 pin_on_exposure=True,
                 search_hints={
                     'poll': ('check wait background command agent task status '
                              'result 后台 命令 任务 状态 结果 等待'),
                     'kill': ('kill stop background command agent process '
                              '停止 终止 后台 进程 任务'),
                 },
                 category='background',
                 description='Unified async handles (poll / kill)',
                 gate='Durable runtime with base, shell, or swarm capability'),
        ToolSpec('retained_agents', _b._build_retained_agents,
                 phase='capability',
                 provides=frozenset({'list_agents', 'manage_agents'}),
                 write_tools=frozenset({'manage_agents'}),
                 discovery_policy='searchable',
                 search_hints={'list_agents': (
                     'show inspect persistent team agents memory roster '
                     '查看 团队 智能体 记忆'),
                     'manage_agents': (
                     'persistent reusable team agent colleague remember '
                     'create assign reset archive long-term subagent '
                     '常驻 长期 子智能体 团队 记忆 派工')},
                 category='swarm', description='Reusable team agents',
                 gate='Durable application runtime only',
                 exposure_gate=lambda ctx: ctx.durable_state_available),
        ToolSpec('evolution_artifact', _b._build_evolution_artifact,
                 phase='capability', provides=frozenset({'evolution_artifact'}),
                 direct_wire_tools=frozenset({'evolution_artifact'}),
                 write_tools=frozenset({'evolution_artifact'}), cacheable_tools=frozenset(),
                 category='evolution', description='Freeze explicitly declared project artifacts',
                 gate='当前项目执行者声明生成物快照，独立评审只读原文',
                 exposure_gate=lambda ctx: bool((ctx.cfg.get('evolution') or {}).get('run_id'))
                     and (ctx.cfg.get('evolution') or {}).get('stage') == 'executing'
                     and bool(ctx.cfg.get('projectEnabled')),
                 handler=_b._handle_evolution_teach),
        ToolSpec('evolution_review', _b._build_evolution_review,
                 phase='capability', provides=frozenset({'evolution_review'}),
                 direct_wire_tools=frozenset({'evolution_review'}), cacheable_tools=frozenset(),
                 category='evolution', description='Submit the independent frozen-source review',
                 gate='当前只读评审提交判断，原生校验仍决定验收',
                 exposure_gate=lambda ctx: bool((ctx.cfg.get('evolution') or {}).get('run_id'))
                     and (ctx.cfg.get('evolution') or {}).get('stage') == 'reviewing',
                 handler=_b._handle_evolution_teach),
        ToolSpec('evolution_source', _b._build_evolution_source,
                 phase='capability', provides=frozenset({'evolution_source'}),
                 idempotent_tools=frozenset({'evolution_source'}), cacheable_tools=frozenset(),
                 result_recovery_by_name={'evolution_source': 'source'},
                 model_input_observer=_b._observe_evolution_source_input,
                 category='evolution', description='Read immutable experiment originals',
                 gate='当前执行者或只读评审的归档原文',
                 exposure_gate=lambda ctx: bool((ctx.cfg.get('evolution') or {}).get('run_id'))
                     and (ctx.cfg.get('evolution') or {}).get('stage') in ('executing', 'reviewing'),
                 handler=_b._handle_evolution_teach),
        # ── per-request custom tools (always last; handlers are task-local) ──
        ToolSpec('custom', _b._build_custom, phase='capability',
                 category='custom',
                 description='Per-request custom tools (handlers via task[_tool_env])',
                 gate='API /api/v4/agent/run 请求自带 tools 时出现'),
    ]
    return specs
