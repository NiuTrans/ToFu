/* ═══════════════════════════════════════════
   i18n.js — Internationalization (Chinese / English)
   Loaded FIRST — before all other scripts.
   ═══════════════════════════════════════════ */

/**
 * Current UI language. Persisted in localStorage.
 * @type {'zh'|'en'}
 */
var _i18nLang = localStorage.getItem('tofu_ui_lang') || 'zh';

/**
 * Translation dictionaries.
 * Key = translation key used in data-i18n attributes and t() calls.
 * Each key maps to { zh: '中文', en: 'English' }.
 */
var _i18n = {
  // ══════════════════════════════════════
  //  Sidebar & Navigation
  // ══════════════════════════════════════
  'sidebar.search': { zh: '搜索对话', en: 'Search conversations' },
  'sidebar.settings': { zh: '设置', en: 'Settings' },

  'sidebar.newChat': { zh: '新对话', en: 'New Chat' },
  'sidebar.uncategorized': { zh: '未分类', en: 'Uncategorized' },
  'sidebar.newFolder': { zh: '新建文件夹', en: 'New Folder' },
  'sidebar.moreFolders': { zh: '展开更多文件夹', en: 'Show more folders' },
  'sidebar.lessFolders': { zh: '收起', en: 'Collapse' },
  'sidebar.allCategorized': { zh: '所有对话都已归类', en: 'All conversations are categorized' },
  'sidebar.folderEmpty': { zh: '文件夹是空的', en: 'Folder is empty' },
  'sidebar.newChatAppear': { zh: '新对话会出现在这里，或从文件夹中移出对话', en: 'New chats will appear here, or move conversations out of folders' },
  'sidebar.clickNewChat': { zh: '点击 New Chat 创建对话，或拖拽对话到此标签', en: 'Click New Chat to create a conversation, or drag one here' },
  'sidebar.feishuConv': { zh: '飞书对话', en: 'Feishu conversation' },
  'sidebar.awaitingInput': { zh: '等待你的输入', en: 'Awaiting your input' },
  'sidebar.translating': { zh: '翻译中…', en: 'Translating…' },
  'sidebar.translatingTag': { zh: '翻译中', en: 'Translating' },
  'sidebar.memoryPrefetch': { zh: '筛选记忆中…', en: 'Filtering memories…' },
  'sidebar.memoryPrefetchTag': { zh: '筛选记忆', en: 'Filtering' },
  'sidebar.answering': { zh: '回答中', en: 'Answering' },
  'sidebar.copyConvId': { zh: '复制会话ID', en: 'Copy conversation ID' },
  'sidebar.refConv': { zh: '引用此对话', en: 'Reference this conversation' },
  'sidebar.moveToFolder': { zh: '移入文件夹', en: 'Move to folder' },
  'sidebar.duplicate': { zh: '复制为新对话', en: 'Duplicate conversation' },
  'sidebar.deleteConv': { zh: '删除对话', en: 'Delete conversation' },

  // ══════════════════════════════════════
  //  Welcome Screen
  // ══════════════════════════════════════
  'welcome.subtitle': { zh: '嫩，但能打 — search, code, browse, trade, and more.', en: 'Soft, but powerful — search, code, browse, trade, and more.' },

  // ══════════════════════════════════════
  //  Toolbar & Input
  // ══════════════════════════════════════
  'toolbar.enhance': { zh: '增强', en: 'Enhance' },
  'toolbar.aiEnhance': { zh: 'AI 增强', en: 'AI Enhance' },
  'toolbar.tools': { zh: '工具', en: 'Tools' },
  'toolbar.externalTools': { zh: '外部工具', en: 'External Tools' },
  'toolbar.mode': { zh: '模式', en: 'Mode' },
  'toolbar.execMode': { zh: '执行模式', en: 'Execution Mode' },
  'toolbar.codeExec': { zh: '代码执行', en: 'Code Execution' },
  'toolbar.codeExecDesc': { zh: '允许 AI 运行代码', en: 'Allow AI to run code' },
  'toolbar.memory': { zh: '记忆经验', en: 'Memory' },
  'toolbar.memoryDesc': { zh: '注入积累的经验', en: 'Inject accumulated experience' },
  'toolbar.autoTranslate': { zh: '自动翻译', en: 'Auto Translate' },
  'toolbar.autoTranslateDesc': { zh: '中英互译 · ⌘⇧K 跳过选中', en: 'CN↔EN translate · ⌘⇧K to skip selected' },
  'toolbar.translateBadge': { zh: '译', en: 'T' },
  'toolbar.browserBridge': { zh: '浏览器桥接', en: 'Browser Bridge' },
  'toolbar.browserBridgeDesc': { zh: '控制浏览器标签页', en: 'Control browser tabs' },
  'toolbar.desktopControl': { zh: '桌面控制', en: 'Desktop Control' },
  'toolbar.desktopControlDesc': { zh: '操作本地应用与文件', en: 'Operate local apps and files' },
  'toolbar.scheduledTasks': { zh: '定时任务', en: 'Scheduled Tasks' },
  'toolbar.scheduledTasksDesc': { zh: '计划任务与 Cron', en: 'Task scheduling & Cron' },
  'toolbar.aiDrawing': { zh: 'AI 绘图', en: 'AI Drawing' },
  'toolbar.aiDrawingDesc': { zh: '对话中生成图片', en: 'Generate images in conversation' },
  'toolbar.humanAICollab': { zh: '人机协作', en: 'Human-AI Collab' },
  'toolbar.humanAICollabDesc': { zh: 'AI 可向你提问寻求指导', en: 'AI can ask you for guidance' },
  'toolbar.swarmAgents': { zh: '蜂群代理', en: 'Swarm Agents' },
  'toolbar.swarmAgentsDesc': {
    zh: '并行子代理分解任务 · 独立工具，无需开启 Project',
    en: 'Parallel sub-agents · works without Project mode',
  },
  'toolbar.autonomousMode': { zh: '自主模式', en: 'Autonomous Mode' },
  'toolbar.autonomousModeDesc': { zh: '自主执行+自我审查循环', en: 'Autonomous execution + self-review loop' },
  'toolbar.autopilot': { zh: '自动驾驶', en: 'Autopilot' },
  'toolbar.autopilotDesc': { zh: '虚拟用户自动回复，直到任务完成', en: 'Virtual user auto-replies until task is done' },
  'toolbar.moreOptions': { zh: '更多选项', en: 'More options' },
  'toolbar.exitCreativeMode': { zh: '退出创作模式 (Esc)', en: 'Exit creative mode (Esc)' },
  'toolbar.generate': { zh: '生成', en: 'Generate' },
  'toolbar.loadingModels': { zh: '正在加载模型…', en: 'Loading models…' },

  // ══════════════════════════════════════
  //  Image Generation
  // ══════════════════════════════════════
  'ig.square': { zh: '1:1 正方形', en: '1:1 Square' },
  'ig.landscape': { zh: '16:9 横屏宽幅', en: '16:9 Landscape' },
  'ig.portrait': { zh: '9:16 竖屏', en: '9:16 Portrait' },
  'ig.classic': { zh: '4:3 经典', en: '4:3 Classic' },
  'ig.tallPortrait': { zh: '3:4 竖版', en: '3:4 Tall portrait' },
  'ig.standardRes': { zh: '1024px · 标准分辨率', en: '1024px · Standard resolution' },
  'ig.hdRes': { zh: '2048px · 高清分辨率', en: '2048px · HD resolution' },
  'ig.single': { zh: '单抽', en: 'Single' },
  'ig.double': { zh: '2连', en: '×2' },
  'ig.quad': { zh: '4连', en: '×4' },
  'ig.singleDesc': { zh: '生成单张图片', en: 'Generate a single image' },
  'ig.doubleDesc': { zh: '同时生成 2 张，多个选择', en: 'Generate 2 images at once' },
  'ig.quadDesc': { zh: '同时生成 4 张，大量出图！', en: 'Generate 4 images at once!' },
  'ig.generateBtn': { zh: '生成 (Enter)', en: 'Generate (Enter)' },
  'ig.placeholder': { zh: '描述你想生成的图片 / 粘贴图片后描述修改内容…', en: 'Describe the image to generate / paste image to edit…' },
  'ig.hint': { zh: 'Enter 生成 · Esc 退出 · 粘贴/拖拽图片可编辑 · 支持中英文', en: 'Enter to generate · Esc to exit · Paste/drag image to edit' },

  // ══════════════════════════════════════
  //  Debug Panel
  // ══════════════════════════════════════
  'debug.copyAll': { zh: '复制全部', en: 'Copy all' },
  'debug.preview': { zh: '预览', en: 'Preview' },
  'debug.previewCompare': { zh: '对比预览', en: 'Compare preview' },
  'debug.clean': { zh: '清理', en: 'Clean' },
  'debug.cleanApply': { zh: '应用规则清理', en: 'Apply rule cleaning' },
  'debug.aiCompress': { zh: 'AI压缩', en: 'AI Compress' },
  'debug.aiCompressDesc': { zh: '用 AI 智能压缩，去除冗余保留关键信息', en: 'AI smart compression, remove redundancy, keep key info' },
  'debug.keepOriginal': { zh: '保持原文', en: 'Keep original' },

  // ══════════════════════════════════════
  //  Settings — Tabs
  // ══════════════════════════════════════
  'settings.title': { zh: '设置', en: 'Settings' },
  'settings.close': { zh: '关闭', en: 'Close' },
  'settings.tabGeneral': { zh: '通用', en: 'General' },
  'settings.tabProviders': { zh: '服务商', en: 'Providers' },
  'settings.tabDisplay': { zh: '显示', en: 'Display' },
  'settings.tabSearch': { zh: '搜索', en: 'Search' },
  'settings.tabNetwork': { zh: '网络', en: 'Network' },
  'settings.tabFeishu': { zh: '飞书', en: 'Feishu' },
  'settings.tabOAuth': { zh: '订阅登录', en: 'OAuth Login' },
  'settings.tabMCP': { zh: 'MCP', en: 'MCP' },
  'settings.tabSkills': { zh: 'Skills', en: 'Skills' },
  'settings.tabAdvanced': { zh: '高级', en: 'Advanced' },
  'skills.title': { zh: 'Skills', en: 'Skills' },
  'skills.tabCatalog': { zh: '市场', en: 'Catalog' },
  'skills.tabInstalled': { zh: '已安装', en: 'Installed' },
  'skills.searchPh': { zh: '搜索 Skills…', en: 'Search skills…' },
  'skills.intro': { zh: 'Skills 是 Claude / OpenClaw / AgentSkills 标准的可重用知识包（SKILL.md + references/scripts/）。可拖入 .zip 或一键安装下方推荐。', en: 'Skills are reusable knowledge packs (SKILL.md + references/scripts/) following the Claude / OpenClaw / AgentSkills standard. Drag-and-drop a .zip or one-click install from below.' },
  'skills.dropZone': { zh: '拖入 .zip 安装本地技能包，或点击选择文件', en: 'Drop a .zip here, or click to choose a file' },
  'skills.installBtn': { zh: '安装', en: 'Install' },
  'skills.installedBtn': { zh: '已安装', en: 'Installed' },
  'skills.uninstallBtn': { zh: '卸载', en: 'Uninstall' },
  'skills.viewFiles': { zh: '查看文件', en: 'View files' },
  'skills.openHomepage': { zh: '主页', en: 'Homepage' },
  'settings.cancel': { zh: '取消', en: 'Cancel' },
  'settings.save': { zh: '保存', en: 'Save' },

  // ══════════════════════════════════════
  //  Settings — General Tab
  // ══════════════════════════════════════
  'settings.language': { zh: '界面语言', en: 'UI Language' },
  'settings.languageDesc': { zh: '切换界面显示语言（中文/英文）', en: 'Switch the UI display language (Chinese/English)' },
  'settings.langZh': { zh: '中文', en: '中文 (Chinese)' },
  'settings.langEn': { zh: 'English', en: 'English' },
  'settings.theme': { zh: '主题', en: 'Theme' },
  'settings.themeDark': { zh: '🌙 暗色', en: '🌙 Dark' },
  'settings.themeLight': { zh: '☀️ 亮色', en: '☀️ Light' },
  'settings.themeTofu': { zh: '🍮 豆腐', en: '🍮 Tofu' },
  'settings.modelParams': { zh: '模型参数', en: 'Model Parameters' },
  'settings.temperature': { zh: '温度 (Temperature)', en: 'Temperature' },
  'settings.maxTokens': { zh: '最大 Token 数', en: 'Max Tokens' },
  'settings.imageMaxWidth': { zh: '图片最大宽度', en: 'Image Max Width' },
  'settings.imageMaxWidthPh': { zh: '0=跟随服务器策略', en: '0 = follow server policy' },
  'settings.defaultThinkingDepth': { zh: '默认思维深度 (Thinking Depth)', en: 'Default Thinking Depth' },
  'settings.thinkingOff': { zh: 'Off — 关闭', en: 'Off' },
  'settings.thinkingMedium': { zh: 'Medium — 中等', en: 'Medium' },
  'settings.thinkingHigh': { zh: 'High — 深度', en: 'High' },
  'settings.thinkingXHigh': { zh: 'xHigh — 超深度 (Claude 4.7+)', en: 'xHigh (Claude 4.7+)' },
  'settings.thinkingMax': { zh: 'Max — 最大', en: 'Max' },
  'settings.defaultThinkingDesc': { zh: '新对话的默认思维深度级别', en: 'Default thinking depth for new conversations' },
  'settings.systemPrompt': { zh: '系统提示词', en: 'System Prompt' },
  'settings.systemPromptPh': { zh: '输入自定义系统提示词...', en: 'Enter custom system prompt...' },
  'settings.featureModules': { zh: '功能模块', en: 'Feature Modules' },
  'settings.tradingModule': { zh: '交易 / 基金模块', en: 'Trading / Fund Module' },
  'settings.tradingModuleDesc': { zh: '交易顾问、基金筛选、自动驾驶、资讯爬虫', en: 'Trading advisor, fund screening, autopilot, news crawler' },
  'settings.pptxTranslateModule': { zh: 'PPT 翻译模块', en: 'PPTX Translation Module' },
  'settings.pptxTranslateModuleDesc': { zh: '上传 PPTX 文件进行全文翻译，保留原始格式', en: 'Upload PPTX files for full translation with formatting preserved' },
  'settings.tradingRestart': { zh: '需要重启服务器才能生效', en: 'Server restart required to take effect' },
  'settings.debugMode': { zh: '调试模式', en: 'Debug Mode' },
  'settings.debugModeDesc': { zh: '显示 trace_id、复制会话 ID 按钮等开发调试信息', en: 'Show trace_id, copy conv ID buttons, and other debug info' },
  'settings.optimizerModule': { zh: '每日优化器', en: 'Daily Optimizer' },
  'settings.optimizerModuleDesc': { zh: '每晚 03:30 分析当日日志并自动提出改进建议（如屏蔽垃圾搜索域名）。关闭后不再运行分析，顶栏 OPTIMIZER 徽章隐藏。已应用的改动会保留直到手动撤销。', en: 'Every night at 03:30 local, analyses the day\'s logs and auto-proposes improvements (e.g. blocking spammy search domains). When off, analysis stops and the top-bar OPTIMIZER badge is hidden. Already-applied changes persist until manually reverted.' },
  'settings.keepToolHistory': { zh: '保留工具调用历史', en: 'Keep Tool Call History' },
  'settings.keepToolHistoryDesc': { zh: '多轮对话时保留完整的工具调用记录（搜索内容、网页抓取结果等），模型能看到之前搜过什么，避免重复调用。关闭可节省 token 但模型会丢失工具上下文', en: 'Preserve full tool call records (search results, fetched pages, etc.) across conversation turns. Model can see what was searched before, avoiding redundant calls. Disable to save tokens but model loses tool context' },
  'settings.inputSendMode': { zh: '输入框发送方式', en: 'Input Send Behavior' },
  'settings.inputSendModeDesc': { zh: '选择触发"发送消息"的按键。Shift+Enter 始终插入换行。', en: 'Choose which key sends the message. Shift+Enter always inserts a newline.' },
  'settings.inputSendModeEnter': { zh: 'Enter 发送 · Ctrl+Enter 换行', en: 'Enter send · Ctrl+Enter newline' },
  'settings.inputSendModeCtrlEnter': { zh: 'Ctrl+Enter 发送 · Enter 换行', en: 'Ctrl+Enter send · Enter newline' },
  'input.hintEnter': { zh: 'Enter 发送 · Ctrl+Enter / Shift+Enter 换行 · 📎 或拖拽上传', en: 'Enter send · Ctrl+Enter / Shift+Enter newline · 📎 or drop files' },
  'input.hintCtrlEnter': { zh: 'Ctrl+Enter 发送 · Enter / Shift+Enter 换行 · 📎 或拖拽上传', en: 'Ctrl+Enter send · Enter / Shift+Enter newline · 📎 or drop files' },

  // ══════════════════════════════════════
  //  Settings — Providers Tab
  // ══════════════════════════════════════
  'settings.providersTitle': { zh: 'API 服务商 & 模型', en: 'API Providers & Models' },
  'settings.autoSetup': { zh: '🚀 自动配置', en: '🚀 Auto Setup' },
  'settings.fromTemplate': { zh: '⚡ 从模板添加', en: '⚡ From Template' },
  'settings.syncTemplate': { zh: '同步模板', en: 'Sync Template' },
  'settings.localProvider': { zh: '🖥️ 本地部署模型', en: '🖥️ Local Deployment' },
  'settings.customProvider': { zh: '+ 自定义服务商', en: '+ Custom Provider' },
  'settings.providersDesc': { zh: '使用「🚀 自动配置」只需填写 API 地址和密钥，系统自动发现模型、检测余额接口和定价。也可从模板添加或手动创建。', en: 'Use "🚀 Auto Setup" — just enter API URL and key, the system auto-discovers models, balance endpoint, and pricing. You can also add from templates or create manually.' },
  'settings.loadingConfig': { zh: '正在加载配置…', en: 'Loading config…' },

  // ══════════════════════════════════════
  //  Self-update (topbar button)
  // ══════════════════════════════════════
  'update.title': { zh: '软件更新', en: 'Software Update' },
  'update.checkTitle': { zh: '检查更新', en: 'Check for updates' },
  'update.availableTitle': { zh: '有可用更新：v%s', en: 'Update available: v%s' },
  'update.checking': { zh: '正在检查更新…', en: 'Checking for updates…' },
  'update.checkFailed': { zh: '无法检查更新，请稍后再试。', en: 'Could not check for updates. Please try again later.' },
  'update.current': { zh: '当前版本', en: 'Current' },
  'update.latest': { zh: '最新版本', en: 'Latest' },
  'update.upToDate': { zh: '✅ 已是最新版本。', en: '✅ You are up to date.' },
  'update.ready': { zh: '有新版本可用。点击下方按钮拉取更新（不会影响你的个人设置）。', en: 'A new version is available. Click below to pull it (your personal settings are untouched).' },
  'update.applyBtn': { zh: '拉取更新', en: 'Pull update' },
  'update.applying': { zh: '正在拉取…', en: 'Pulling…' },
  'update.applyFailed': { zh: '更新失败。', en: 'Update failed.' },
  'update.pulled': { zh: '已更新到 %s。需要重启服务器以生效。', en: 'Updated to %s. A restart is required to apply it.' },
  'update.depsInstalled': { zh: '✅ 已安装新的依赖包。', en: '✅ New dependencies installed.' },
  'update.depsFailed': { zh: '⚠️ 已更新到 %s，但安装新依赖失败。请手动运行 pip install -r requirements.txt 后再重启。', en: '⚠️ Updated to %s, but installing new dependencies failed. Run "pip install -r requirements.txt" manually, then restart.' },
  'update.restartBtn': { zh: '立即重启', en: 'Restart now' },
  'update.restarting': { zh: '正在重启…', en: 'Restarting…' },
  'update.restartHint': { zh: '重启期间页面会短暂不可用，完成后会自动刷新。', en: 'The page will be briefly unavailable and will auto-refresh once back.' },
  'update.restartWait': { zh: '正在等待服务器恢复…', en: 'Waiting for the server to come back…' },
  'update.restartTimeout': { zh: '服务器重启耗时较长，请手动刷新页面。', en: 'Server is taking a while — please refresh manually.' },
  'update.dirty': { zh: '⚠️ 检测到对受版本控制的源码文件的本地改动，已阻止自动更新（不会自动暂存或覆盖）。请先提交或还原以下文件：', en: '⚠️ Local changes to tracked source files were detected — the update is blocked (we never auto-stash or overwrite). Commit or revert these first:' },
  'update.noGit': { zh: '当前不是 git 检出目录，无法原地更新。请重新运行 install.sh。', en: 'This is not a git checkout — in-place update is unavailable. Re-run install.sh.' },
  'settings.loadingFailed': { zh: '加载服务器配置失败。请检查服务器是否正在运行。', en: 'Failed to load server config. Please check if the server is running.' },
  'settings.noProviders': { zh: '还没有配置服务商。点击"+ 自定义服务商"开始添加。', en: 'No providers configured. Click "+ Custom Provider" to start.' },
  'settings.keys': { zh: '个密钥', en: 'keys' },
  'settings.models': { zh: '个模型', en: 'models' },
  'settings.disabled': { zh: '已禁用', en: 'Disabled' },
  'settings.displayName': { zh: '显示名称', en: 'Display Name' },
  'settings.baseUrl': { zh: 'API 地址 (Base URL)', en: 'API URL (Base URL)' },
  'settings.apiKeys': { zh: 'API 密钥', en: 'API Keys' },
  'settings.apiKeysHint': { zh: '安全存储；点击 👁 切换可见性', en: 'Securely stored; click 👁 to toggle visibility' },
  'settings.addApiKey': { zh: '+ 添加密钥', en: '+ Add Key' },
  'settings.addApiKeyTitle': { zh: '新增一个 API 密钥', en: 'Add a new API key' },
  'settings.deleteApiKeyTitle': { zh: '删除该密钥', en: 'Delete this key' },
  'settings.showHideKeyTitle': { zh: '切换密钥可见性', en: 'Toggle key visibility' },
  'settings.noApiKeys': { zh: '暂无 API 密钥。点击右上角 + 添加。', en: 'No API keys yet. Click + above to add one.' },
  'settings.noApiKeysLocal': { zh: '暂无 API 密钥（多数本地部署不需要鉴权）。', en: 'No API keys (most local deployments don\'t need auth).' },
  'settings.balanceUrl': { zh: '余额查询地址', en: 'Balance Query URL' },
  'settings.balanceUrlHint': { zh: '可选 — OpenAI 兼容的账单接口', en: 'Optional — OpenAI-compatible billing endpoint' },
  'settings.checkBalance': { zh: '查询 ▸', en: 'Check ▸' },
  'settings.modelsPath': { zh: '模型发现路径', en: 'Models Discovery Path' },
  'settings.modelsPathHint': { zh: '可选 — 默认在 Base URL 后追加 /models', en: 'Optional — defaults to Base URL + /models' },
  'settings.customHeaders': { zh: '自定义请求头', en: 'Custom Headers' },
  'settings.customHeadersHint': { zh: '可选 — 每行一对，附加到本服务商的所有请求', en: 'Optional — one pair per row, appended to every request to this provider' },
  'settings.addHeader': { zh: '+ 添加请求头', en: '+ Add Header' },
  'settings.addHeaderTitle': { zh: '新增一行请求头', en: 'Add a new header row' },
  'settings.deleteHeaderTitle': { zh: '删除该请求头', en: 'Delete this header' },
  'settings.headerNamePlaceholder': { zh: 'Header 名称', en: 'Header-Name' },
  'settings.headerValuePlaceholder': { zh: 'Header 值', en: 'value' },
  'settings.noHeaders': { zh: '暂无自定义请求头。点击右上角 + 添加。', en: 'No custom headers yet. Click + above to add one.' },
  'settings.thinkingFormat': { zh: '思维参数格式', en: 'Thinking Parameter Format' },
  'settings.thinkingFormatAuto': { zh: '自动检测（按模型名称）', en: 'Auto-detect (by model name)' },
  'settings.thinkingFormatEnable': { zh: 'enable_thinking（LongCat/Qwen/Gemini 风格）', en: 'enable_thinking (LongCat/Qwen/Gemini style)' },
  'settings.thinkingFormatType': { zh: 'thinking.type（Doubao/Claude 风格）', en: 'thinking.type (Doubao/Claude style)' },
  'settings.thinkingFormatNone': { zh: '不发送思维参数', en: 'Do not send thinking parameter' },
  'settings.enabled': { zh: '启用', en: 'Enabled' },
  'settings.deleteProvider': { zh: '🗑 删除服务商', en: '🗑 Delete Provider' },
  'settings.modelList': { zh: '模型列表', en: 'Model List' },
  'settings.autoDiscover': { zh: '🔍 自动发现', en: '🔍 Auto Discover' },
  'settings.addModel': { zh: '+ 添加模型', en: '+ Add Model' },
  'settings.noModels': { zh: '还没有配置模型。点击"🔍 自动发现"自动检测可用模型，或点击"+ 添加模型"手动添加。', en: 'No models configured. Click "🔍 Auto Discover" to detect available models, or click "+ Add Model" to add manually.' },
  'settings.autoDiscoverHint': { zh: '从 /v1/models 接口自动发现模型', en: 'Auto-discover models from /v1/models endpoint' },
  'settings.aliases': { zh: '别名：', en: 'Aliases:' },
  'settings.addAlias': { zh: '+ 别名', en: '+ Alias' },
  'settings.apply': { zh: '应用', en: 'Apply' },
  // ── Access Matrix (per-key × per-model capability grid) ──
  'settings.matrixViewMatrix': { zh: '访问矩阵', en: 'Access Matrix' },
  'settings.matrixViewCards': { zh: '卡片视图', en: 'Card View' },
  'settings.matrixToggleHint': { zh: '按「密钥 × 模型」逐格管理访问、别名、限速与能力', en: 'Manage access, aliases, RPM and capabilities per (key × model) cell' },
  'settings.matrixModelCol': { zh: '模型 ＼ 密钥', en: 'Model ＼ Key' },
  'settings.matrixNoKeys': { zh: '该服务商还没有 API 密钥。', en: 'This provider has no API keys yet.' },
  'settings.matrixBlankKey': { zh: '（无密钥）', en: '(no key)' },
  'settings.matrixRenameKey': { zh: '为此密钥命名（如 高配额 / 测试）', en: 'Name this key (e.g. high-quota / test)' },
  'settings.matrixLegendOn': { zh: '已开放', en: 'Granted' },
  'settings.matrixLegendOff': { zh: '已禁用', en: 'Disabled' },
  'settings.matrixLegendOverride': { zh: '有覆盖', en: 'Overridden' },
  'settings.matrixGlobalToggle': { zh: '全局启用 / 禁用该模型（影响所有密钥）', en: 'Globally enable/disable this model (all keys)' },
  'settings.matrixClickEnable': { zh: '点击：对该密钥开放此模型', en: 'Click: grant this key access to this model' },
  'settings.matrixClickDisable': { zh: '点击：对该密钥禁用此模型', en: 'Click: deny this key access to this model' },
  'settings.matrixEditCell': { zh: '为此（密钥 × 模型）设置专属别名 / 限速 / 能力', en: 'Set per-cell aliases / RPM / capabilities' },
  'settings.matrixCellEnabled': { zh: '该密钥可访问此模型', en: 'This key may access this model' },
  'settings.matrixOverrideRpm': { zh: '覆盖限速 (RPM)', en: 'Override RPM' },
  'settings.matrixOverrideAlias': { zh: '覆盖别名', en: 'Override aliases' },
  'settings.matrixOverrideCaps': { zh: '覆盖能力', en: 'Override capabilities' },
  'settings.matrixInheritHint': { zh: '默认继承：', en: 'Inherits:' },
  'settings.matrixNoAlias': { zh: '无别名', en: 'no aliases' },
  // ── Access Matrix: probe & recommend ──
  'settings.matrixProbe': { zh: '探测推荐', en: 'Probe & Recommend' },
  'settings.matrixProbeHint': { zh: '逐格发送最小请求，检测每个密钥可访问哪些模型/别名。在后台运行并持久化——关闭设置也不会中断，重开后自动恢复', en: 'Send a tiny request per cell to detect which models/aliases each key can reach. Runs in the background and is persisted — closing Settings won\'t interrupt it; it resumes on reopen' },
  'settings.matrixRetest': { zh: '重新探测', en: 'Retest' },
  'settings.matrixProbing': { zh: '正在后台探测…', en: 'Probing in background…' },
  'settings.matrixProbeFailed': { zh: '探测失败', en: 'Probe failed' },
  'settings.matrixNothingToProbe': { zh: '没有可探测的密钥或模型', en: 'No keys or models to probe' },
  'settings.matrixOkCount': { zh: '可用', en: 'reachable' },
  'settings.matrixFlaggedCount': { zh: '建议禁用', en: 'flagged' },
  'settings.matrixApplyRec': { zh: '应用推荐', en: 'Apply Recommendations' },
  'settings.matrixApplyHint': { zh: '禁用所有「建议禁用」的格子（每个别名独立处理：失效别名单独禁用，根模型与其它别名保持可用）', en: 'Disable every flagged cell (each alias is independent: a dead alias is disabled on its own; the root and other aliases stay reachable)' },
  'settings.matrixAliasTag': { zh: '别名', en: 'alias' },
  'settings.matrixEditorSub': { zh: '限速与能力作用于整行模型；别名的开关请直接点击别名行的格子。', en: 'RPM & capabilities apply to the whole model entry; toggle an alias on/off by clicking its own row\'s cells.' },
  'settings.matrixAliasOne': { zh: '别名', en: 'alias' },
  'settings.matrixAliasMany': { zh: '别名', en: 'aliases' },
  'settings.matrixAliasCountHint': { zh: '该模型的别名各自路由到不同的上游模型，可逐个开关', en: 'Each alias routes to a different upstream model and can be toggled independently' },
  'settings.matrixAttempts': { zh: '探测次数', en: 'Attempts' },
  'settings.matrixAttemptsHint': { zh: '每个格子探测多次以过滤偶发的假 429；任一次成功即视为可用', en: 'Probe each cell several times to filter out false 429s; a single success counts as reachable' },
  'settings.matrixApplied': { zh: '已应用：禁用了 {n} 个格子', en: 'Applied: disabled {n} cell(s)' },
  'settings.matrixNothingApplied': { zh: '没有需要禁用的格子', en: 'No cells needed disabling' },
  'settings.matrixClearProbe': { zh: '清除探测结果', en: 'Clear results' },
  'settings.probeOk': { zh: '可用', en: 'Reachable' },
  'settings.probeRateLimited': { zh: '限流 (429)', en: 'Rate-limited (429)' },
  'settings.probeUnauthorized': { zh: '无权限', en: 'Unauthorized' },
  'settings.probeNotFound': { zh: '模型不存在', en: 'Model not found' },
  'settings.probeUnavailable': { zh: '不可用 / 超时', en: 'Unavailable / timeout' },
  'settings.probeError': { zh: '错误', en: 'Error' },
  'settings.edit': { zh: '编辑', en: 'Edit' },
  'settings.delete': { zh: '删除', en: 'Delete' },
  'settings.free': { zh: '免费', en: 'Free' },
  'settings.noPricing': { zh: '暂无价格数据', en: 'No pricing data' },
  'settings.input': { zh: '输入', en: 'Input' },
  'settings.output': { zh: '输出', en: 'Output' },
  'settings.perMillionTokens': { zh: '每百万 Token', en: 'per million tokens' },
  'settings.balance': { zh: '余额', en: 'Balance' },
  'settings.balanceClickRefresh': { zh: '余额（点击刷新）', en: 'Balance (click to refresh)' },
  'settings.used': { zh: '已用', en: 'Used' },
  'settings.remaining': { zh: '剩余', en: 'Remaining' },
  'settings.quota': { zh: '额度', en: 'Quota' },
  'settings.checking': { zh: '查询中…', en: 'Checking…' },
  'settings.apiKeysHintLocal': { zh: '可选 — 多数本地部署不需要鉴权', en: 'Optional — most local deployments don\'t need auth' },
  'settings.checkBalanceTitle': { zh: '查询余额', en: 'Check balance' },
  'settings.thinkingFormatHint': { zh: '默认自动检测 — 仅当端点使用非标准格式时需配置', en: 'Auto-detected by default — configure only if the endpoint uses a non-standard format' },
  'settings.syncTemplateTitle': { zh: '从内置模板同步新增模型', en: 'Sync new models from the built-in template' },
  'settings.probeAllEndpoints': { zh: '🔍 探测全部端点', en: '🔍 Probe All Endpoints' },
  'settings.probeAllEndpointsTitle': { zh: '并行探测所有端点并合并模型列表', en: 'Probe all endpoints in parallel and merge their model lists' },
  'settings.modelEnabledTitle': { zh: '该模型已启用 — 点击禁用', en: 'Model enabled — click to disable' },
  'settings.modelDisabledTitle': { zh: '该模型已禁用 — 点击启用', en: 'Model disabled — click to enable' },
  'settings.editTitle': { zh: '编辑', en: 'Edit' },
  'settings.deleteTitle': { zh: '删除', en: 'Delete' },
  'settings.endpointsSuffix': { zh: '个端点', en: 'endpoints' },
  'settings.moreEndpointsSuffix': { zh: '个端点', en: 'more' },
  // Local endpoint manager (inside provider card)
  'settings.endpointUrlList': { zh: '端点 URL 列表', en: 'Endpoint URL List' },
  'settings.addEndpoint': { zh: '+ 添加端点', en: '+ Add Endpoint' },
  'settings.addEndpointTitle': { zh: '新增一行端点', en: 'Add a new endpoint row' },
  'settings.bulkEdit': { zh: '📝 批量编辑', en: '📝 Bulk Edit' },
  'settings.bulkEditTitle': { zh: '一次性粘贴或编辑全部 URL', en: 'Paste or edit all URLs at once' },
  'settings.probeAll': { zh: '🔍 探测全部', en: '🔍 Probe All' },
  'settings.probeAllTitle': { zh: '并行探测全部端点并合并模型列表', en: 'Probe all endpoints in parallel and merge their model lists' },
  'settings.clearAll': { zh: '🗑 清空', en: '🗑 Clear' },
  'settings.clearAllTitle': { zh: '清空全部端点', en: 'Clear all endpoints' },
  'settings.localEndpointsHint': { zh: '调度器会在多个端点之间负载均衡，单个端点宕机会自动绕开。私网/公网 IP 都会自动加入代理白名单。', en: 'The scheduler load-balances across endpoints and routes around any that go down. Both private and public IPs are added to the proxy bypass list automatically.' },
  'settings.testEndpointTitle': { zh: '立即刷新该端点的实时指标', en: 'Refresh live metrics for this endpoint now' },
  'settings.deleteEndpointTitle': { zh: '删除该端点', en: 'Delete this endpoint' },
  // Inline "thinking" toggle on model cards
  'settings.thinking': { zh: '思考', en: 'thinking' },
  'settings.enableThinkingHint': {
    zh: '点击启用思考能力（开启后聊天时会显示思维深度选择器）',
    en: 'Click to enable thinking capability (chat input will show the thinking-depth picker)'
  },
  'settings.disableThinkingHint': {
    zh: '点击关闭思考能力',
    en: 'Click to disable thinking capability'
  },
  // Live per-endpoint metrics (auto-polled, recorded from real traffic)
  'settings.epHealthy':       { zh: '运行正常', en: 'Healthy' },
  'settings.epRecentFailure': { zh: '近期失败', en: 'Recent failures' },
  'settings.epNoTraffic':     { zh: '尚无流量 · 等待第一次请求', en: 'No traffic yet — waiting for first request' },
  'settings.epLastSeen':      { zh: '最近', en: 'last' },
  'settings.epLatency':       { zh: '延迟', en: 'Latency' },
  'settings.epThroughput':    { zh: '吞吐', en: 'Throughput' },
  'settings.epSuccessRate':   { zh: '成功率', en: 'Success' },
  'settings.epInflight':      { zh: '在途', en: 'inflight' },
  'settings.epRequests':      { zh: '次请求', en: 'requests' },
  'settings.epAllFailed': { zh: '全部端点异常', en: 'All endpoints failing' },
  'settings.epAllOk': { zh: '全部正常', en: 'All OK' },
  'settings.epPartialOk': { zh: '{ok}/{total} 个端点正常', en: '{ok}/{total} endpoints OK' },
  'settings.epNotProbed': { zh: '尚未探测', en: 'Not probed yet' },
  'settings.epProbing': { zh: '探测中…', en: 'Probing…' },
  'settings.epOk': { zh: '正常', en: 'OK' },
  'settings.epError': { zh: '异常', en: 'Error' },
  'settings.epProbeFailed': { zh: '探测失败', en: 'Probe failed' },
  'settings.relJustNow': { zh: '刚刚', en: 'just now' },
  'settings.relSecAgo': { zh: '秒前', en: 's ago' },
  'settings.relMinAgo': { zh: '分钟前', en: 'min ago' },
  'settings.relHourAgo': { zh: '小时前', en: 'h ago' },
  'settings.relDayAgo': { zh: '天前', en: 'd ago' },

  // ══════════════════════════════════════
  //  Settings — Display / Preset Tab
  // ══════════════════════════════════════
  'settings.imageGen': { zh: '图片生成', en: 'Image Generation' },
  'settings.showAll': { zh: '全部显示', en: 'Show All' },
  'settings.hideAll': { zh: '全部隐藏', en: 'Hide All' },
  'settings.igDesc': { zh: '选择在图片生成选择器中显示哪些模型。隐藏的模型仍然可用，只是不会出现在选择器中。', en: 'Choose which models appear in the image generation picker. Hidden models are still usable, just not shown in the picker.' },
  'settings.modelDropdown': { zh: '模型下拉列表', en: 'Model Dropdown' },
  'settings.modelDropdownDesc': { zh: '选择在模型切换下拉列表中显示哪些模型。隐藏的模型仍然可用，只是不会出现在下拉列表中。', en: 'Choose which models appear in the model switcher dropdown. Hidden models are still usable.' },
  'settings.modelDefaults': { zh: '模型默认', en: 'Model Defaults' },
  'settings.modelDefaultsDesc': { zh: '配置自动回退模型和预设的默认模型。当主模型请求失败时，系统将自动切换到回退模型继续生成。预设默认模型用于快捷切换时的模型选择。留空表示禁用或使用系统默认。', en: 'Configure fallback model and default models. When the primary model fails, the system switches to the fallback model. Leave empty to disable or use system defaults.' },
  'settings.fallbackModel': { zh: '回退模型', en: 'Fallback Model' },
  'settings.fallbackModelHint': { zh: '主模型失败时自动切换', en: 'Auto-switch when primary model fails' },
  'settings.disableFallback': { zh: '（禁用自动回退）', en: '(Disable auto-fallback)' },
  'settings.defaultModel': { zh: '默认模型', en: 'Default Model' },
  'settings.useEnvVar': { zh: '（使用环境变量）', en: '(Use environment variable)' },

  // ══════════════════════════════════════
  //  Settings — Search Tab
  // ══════════════════════════════════════
  'settings.searchFetch': { zh: '搜索与抓取', en: 'Search & Fetch' },
  'settings.llmContentFilter': { zh: 'LLM 内容过滤', en: 'LLM Content Filter' },
  'settings.llmContentFilterDesc': { zh: '抓取网页后用模型过滤无关内容（导航栏、广告等）。关闭可显著提升抓取速度并节省 token，但搜索质量会下降', en: 'Filter irrelevant content (nav, ads) after fetching pages. Turning off improves speed and saves tokens, but reduces search quality.' },
  'settings.searchFetchParams': { zh: '搜索与抓取参数', en: 'Search & Fetch Parameters' },
  'settings.fetchTopN': { zh: '抓取前 N 条', en: 'Fetch Top N' },
  'settings.fetchTopNHint': { zh: '搜索后自动抓取排名靠前的网页', en: 'Auto-fetch top-ranked pages after search' },
  'settings.fetchTimeout': { zh: '抓取超时', en: 'Fetch Timeout' },
  'settings.fetchTimeoutHint': { zh: '秒', en: 'seconds' },
  'settings.maxCharsSearch': { zh: '最大字符数', en: 'Max Characters' },
  'settings.maxCharsSearchHint': { zh: '搜索结果页面', en: 'Search result pages' },
  'settings.maxCharsDirect': { zh: '最大字符数', en: 'Max Characters' },
  'settings.maxCharsDirectHint': { zh: '直接抓取 URL', en: 'Direct URL fetch' },
  'settings.maxCharsPdf': { zh: '最大字符数', en: 'Max Characters' },
  'settings.maxCharsPdfHint': { zh: 'PDF 文件，0=不限制', en: 'PDF files, 0=unlimited' },
  'settings.maxBytes': { zh: '最大下载大小', en: 'Max Download Size' },
  'settings.maxBytesHint': { zh: '字节，默认 20MB', en: 'bytes, default 20MB' },
  'settings.blockedDomains': { zh: '屏蔽域名', en: 'Blocked Domains' },
  'settings.blockedDomainsDesc': { zh: '抓取器不会访问的域名，每行一个。', en: 'Domains the fetcher will not visit, one per line.' },

  // ══════════════════════════════════════
  //  Settings — Translation Tab
  // ══════════════════════════════════════
  'settings.tabTranslate': { zh: '翻译', en: 'Translation' },
  'settings.mtService': { zh: '机器翻译服务', en: 'Machine Translation Service' },
  'settings.mtServiceDesc': { zh: '配置专用机器翻译 API，比 LLM 翻译更快、更便宜。<br>未配置或关闭时，翻译将自动使用 LLM cheap 模型。', en: 'Configure a dedicated machine translation API — faster and cheaper than LLM translation.<br>When not configured or disabled, translation falls back to LLM cheap model.' },
  'settings.mtEnable': { zh: '启用机器翻译', en: 'Enable Machine Translation' },
  'settings.mtEnableDesc': { zh: '开启后，翻译将优先使用机器翻译 API，失败时自动回退到 LLM', en: 'When enabled, translation uses the MT API first, falling back to LLM on failure' },
  'settings.mtProvider': { zh: '翻译服务商', en: 'Translation Provider' },
  'settings.mtProviderNiutrans': { zh: '小牛翻译 NiuTrans', en: 'NiuTrans' },
  'settings.mtProviderCustom': { zh: '自定义 Custom', en: 'Custom' },
  'settings.mtNiutransName': { zh: '小牛翻译', en: 'NiuTrans' },
  'settings.mtNiutransDesc': { zh: '支持 400+ 语种互译 · 中英日韩高质量翻译 · 东北大学 NLP 实验室', en: '400+ language pairs · High-quality CJK translation · NEU NLP Lab' },
  'settings.mtApplyKey': { zh: '申请 API Key', en: 'Get API Key' },
  'settings.mtApiKeyPh': { zh: '在小牛翻译控制台 → API 管理中获取', en: 'Get from NiuTrans console → API Management' },
  'settings.mtAppIdLabel': { zh: 'App ID', en: 'App ID' },
  'settings.mtAppIdHint': { zh: '可选，v2 签名鉴权', en: 'Optional, v2 signed auth' },
  'settings.mtAppIdPh': { zh: '留空使用简单 API Key 鉴权 (v1)', en: 'Leave empty for simple API Key auth (v1)' },
  'settings.mtApiUrlLabel': { zh: 'API 地址', en: 'API URL' },
  'settings.mtApiUrlHint': { zh: '可选', en: 'Optional' },
  'settings.mtApiUrlPh': { zh: '留空使用默认地址', en: 'Leave empty for default URL' },
  'settings.mtTestBtn': { zh: '测试连接', en: 'Test Connection' },
  'settings.mtTesting': { zh: '⏳ 测试中…', en: '⏳ Testing…' },
  'settings.mtTestOk': { zh: '✅ 连接成功：', en: '✅ Connected: ' },
  'settings.mtTestFail': { zh: '未知错误', en: 'Unknown error' },
  'settings.mtTestReqFail': { zh: '❌ 请求失败: ', en: '❌ Request failed: ' },
  'settings.mtCustomName': { zh: '自定义服务商', en: 'Custom Provider' },
  'settings.mtCustomDesc': { zh: '接入其他兼容 NiuTrans API 格式的翻译服务', en: 'Connect to other translation services compatible with NiuTrans API format' },
  'settings.mtCustomApiKeyPh': { zh: '翻译服务 API Key', en: 'Translation service API Key' },
  'settings.mtCustomAppIdPh': { zh: '如需签名鉴权则填写', en: 'Fill in if signed auth is required' },
  'settings.mtCustomApiUrlHint': { zh: '必填', en: 'Required' },

  // ══════════════════════════════════════
  //  Settings — Network Tab
  // ══════════════════════════════════════
  'settings.httpProxy': { zh: 'HTTP 代理', en: 'HTTP Proxy' },
  'settings.httpProxyDesc': { zh: '配置用于所有出站请求（LLM API、网页搜索、页面抓取）的 HTTP/HTTPS 代理。留空则使用系统环境变量（http_proxy / https_proxy）。修改立即生效，无需重启。', en: 'Configure HTTP/HTTPS proxy for all outbound requests (LLM API, web search, page fetch). Leave empty to use system env vars (http_proxy / https_proxy). Changes take effect immediately.' },
  'settings.httpsProxy': { zh: 'HTTPS 代理', en: 'HTTPS Proxy' },
  'settings.proxyBypassTitle': { zh: '不代理域名', en: 'Proxy Bypass Domains' },
  'settings.proxyBypassDesc': { zh: '在此添加不需要走代理的域名后缀或主机名（每行一个，后缀匹配）。匹配的请求会完全绕过 HTTP 代理。', en: 'Add domain suffixes or hostnames that should bypass the proxy (one per line, suffix matching). Matching requests bypass the HTTP proxy entirely.' },
  'settings.proxyBypassTip': { zh: '💡 提示：内网地址和 LLM API 域名都应加在这里。企业/VPN 代理会静默断开长连接（SSE 流），导致 BrokenPipeError，添加对应域名即可解决。也可通过环境变量 PROXY_BYPASS_DOMAINS（逗号分隔）配置，两处合并生效。', en: '💡 Tip: Internal addresses and LLM API domains should be added here. Corporate/VPN proxies may silently close long connections (SSE streams), causing BrokenPipeError — adding the domain here fixes it. Can also be set via PROXY_BYPASS_DOMAINS env var (comma-separated).' },
  'settings.bypassDomains': { zh: '绕过域名', en: 'Bypass Domains' },
  'settings.bypassDomainsHint': { zh: '每行一个，后缀匹配 — 例如 .your-corp.com', en: 'One per line, suffix matching — e.g. .your-corp.com' },

  // ══════════════════════════════════════
  //  Settings — Feishu Tab
  // ══════════════════════════════════════
  'settings.feishuBot': { zh: '飞书 (Lark) 机器人', en: 'Feishu (Lark) Bot' },
  'settings.connectionStatus': { zh: '连接状态', en: 'Connection Status' },
  'settings.loadingStatus': { zh: '加载状态中…', en: 'Loading status…' },
  'settings.credentials': { zh: '凭证', en: 'Credentials' },
  'settings.defaultProjectPath': { zh: '默认项目路径', en: 'Default Project Path' },
  'settings.workspaceRoot': { zh: '工作空间根目录', en: 'Workspace Root' },
  'settings.workspace': { zh: '工作空间', en: 'Workspace' },
  'settings.accessControl': { zh: '访问控制', en: 'Access Control' },
  'settings.allowedUsers': { zh: '允许的用户', en: 'Allowed Users' },
  'settings.allowedUsersHint': { zh: '飞书 open_id，每行一个 — 留空表示允许所有人', en: 'Feishu open_id, one per line — leave empty to allow everyone' },
  'settings.credModRestart': { zh: '凭证修改需要重启服务器才能生效', en: 'Credential changes require server restart' },

  // ══════════════════════════════════════
  //  Settings — OAuth Tab
  // ══════════════════════════════════════
  'settings.oauthTitle': { zh: '订阅登录', en: 'OAuth Login' },
  'settings.oauthDesc': { zh: '使用 ChatGPT Plus / Claude Pro 订阅账号登录，无需 API Key，直接使用订阅额度。', en: 'Login with ChatGPT Plus / Claude Pro subscription — no API Key needed, use your subscription quota directly.' },
  'settings.oauthChinaWarn': { zh: '⚠️ 中国用户需要全程代理（Clash/VPN），授权弹窗和服务器换 token 都需要能访问外网。建议在本地浏览器无痕窗口中完成授权。', en: '⚠️ Users in China need a proxy (Clash/VPN) throughout. Both the auth popup and server token exchange require internet access. Use an incognito window.' },
  'settings.notLoggedIn': { zh: '未登录', en: 'Not logged in' },
  'settings.loginClaude': { zh: '登录 Claude', en: 'Login Claude' },
  'settings.loginChatGPT': { zh: '登录 ChatGPT', en: 'Login ChatGPT' },
  'settings.logout': { zh: '退出', en: 'Logout' },
  'settings.claudeDesc': { zh: '登录 Claude 订阅，使用 Sonnet / Opus 等模型，无需 API Key。', en: 'Login with Claude subscription to use Sonnet / Opus models without API Key.' },
  'settings.codexDesc': { zh: '登录 ChatGPT 订阅，使用 Codex 模型，请求自动转换为 Responses API 格式。', en: 'Login with ChatGPT subscription to use Codex models, auto-converted to Responses API format.' },
  'settings.popupBlocked': { zh: '如弹窗无法打开，请复制链接到开了代理的浏览器无痕窗口中打开：', en: 'If the popup is blocked, copy the link and open it in an incognito window with proxy enabled:' },
  'settings.copyLink': { zh: '复制链接', en: 'Copy Link' },
  'settings.copied': { zh: '✓ 已复制', en: '✓ Copied' },
  'settings.authCodeHint': { zh: '授权成功后页面显示授权码，复制 code#state 粘贴到下方：', en: 'After authorization, copy the code#state from the page and paste below:' },
  'settings.callbackHint': { zh: '授权成功后，复制浏览器地址栏中的回调 URL 粘贴到下方：', en: 'After authorization, copy the callback URL from the browser address bar below:' },
  'settings.submit': { zh: '提交', en: 'Submit' },
  'settings.oauthInstructions': { zh: '使用说明', en: 'Instructions' },

  // ══════════════════════════════════════
  //  Settings — MCP Tab
  // ══════════════════════════════════════
  'settings.searchApps': { zh: '搜索 Apps…', en: 'Search Apps…' },
  'settings.loading': { zh: '正在加载…', en: 'Loading…' },
  'settings.installed': { zh: '已安装', en: 'Installed' },
  'settings.connectAll': { zh: '全部连接', en: 'Connect All' },
  'settings.manualAdd': { zh: '⚙ 手动添加自定义服务器', en: '⚙ Manually add custom server' },
  'settings.name': { zh: '名称', en: 'Name' },
  'settings.transport': { zh: '传输协议', en: 'Transport' },
  'settings.transportStdio': { zh: 'stdio (本地命令)', en: 'stdio (local command)' },
  'settings.transportSSE': { zh: 'SSE (远程 URL)', en: 'SSE (remote URL)' },
  'settings.command': { zh: '命令', en: 'Command' },
  'settings.args': { zh: '参数', en: 'Arguments' },
  'settings.argsHint': { zh: '每行一个', en: 'One per line' },
  'settings.envVars': { zh: '环境变量', en: 'Environment Variables' },
  'settings.envVarsHint': { zh: '每行 KEY=VALUE', en: 'One KEY=VALUE per line' },
  'settings.description': { zh: '描述', en: 'Description' },
  'settings.optional': { zh: '可选', en: 'Optional' },
  'settings.saveAndConnect': { zh: '保存并连接', en: 'Save & Connect' },
  'settings.installAndConnect': { zh: '安装并连接', en: 'Install & Connect' },

  // ══════════════════════════════════════
  //  Settings — Advanced Tab
  // ══════════════════════════════════════
  'settings.importExport': { zh: '导入 / 导出', en: 'Import / Export' },
  'settings.importExportDesc': { zh: '将所有服务器端配置导出为 JSON，或从文件导入。', en: 'Export all server config as JSON, or import from file.' },
  'settings.exportJson': { zh: '⬇ 导出 JSON', en: '⬇ Export JSON' },
  'settings.importJson': { zh: '⬆ 导入 JSON', en: '⬆ Import JSON' },
  'settings.pricingOverride': { zh: '价格覆盖', en: 'Pricing Override' },
  'settings.pricingOverrideDesc': { zh: '模型定价（美元 / 每百万 Token）。编辑以下 JSON 进行自定义。', en: 'Model pricing (USD / per million tokens). Edit the JSON below to customize.' },
  'settings.localCache': { zh: '本地缓存', en: 'Local Cache' },
  'settings.localCacheDesc': { zh: '对话缓存在 IndexedDB 中以实现即时加载。服务器始终是数据的唯一来源。', en: 'Conversations are cached in IndexedDB for instant loading. The server is always the source of truth.' },
  'settings.clearCache': { zh: '清除缓存', en: 'Clear Cache' },
  'settings.serverInfo': { zh: '服务器信息', en: 'Server Info' },
  'settings.status': { zh: '状态', en: 'Status' },

  // ══════════════════════════════════════
  //  Settings — Auto Setup Modal
  // ══════════════════════════════════════
  'settings.autoSetupTitle': { zh: '🚀 自动配置服务商', en: '🚀 Auto Setup Provider' },
  'settings.autoSetupDesc': { zh: '只需填写 API 地址和密钥，系统将自动发现模型、检测余额接口、识别服务商品牌并获取定价信息。', en: 'Just enter the API URL and key — the system will auto-discover models, detect balance endpoint, identify provider brand, and fetch pricing info.' },
  'settings.autoSetupUrl': { zh: 'API 地址 (Base URL)', en: 'API URL (Base URL)' },
  'settings.autoSetupUrlHint': { zh: '填写 OpenAI 兼容的 API 地址，通常以 /v1 结尾', en: 'Enter an OpenAI-compatible API URL, usually ending with /v1' },
  'settings.autoSetupKey': { zh: 'API 密钥', en: 'API Key' },
  'settings.autoSetupModelsPath': { zh: '模型发现路径', en: 'Models Discovery Path' },
  'settings.autoSetupModelsPathHint': { zh: '可选 — 默认 /models', en: 'Optional — defaults to /models' },
  'settings.startProbe': { zh: '🔍 开始探测', en: '🔍 Start Probe' },
  'settings.probing': { zh: '⏳ 正在探测…', en: '⏳ Probing…' },
  'settings.discoveringModels': { zh: '正在发现模型… 这可能需要几秒钟', en: 'Discovering models… This may take a few seconds' },
  'settings.fillUrl': { zh: '请填写 API 地址', en: 'Please enter API URL' },
  'settings.fillKey': { zh: '请填写 API 密钥', en: 'Please enter API Key' },
  'settings.probeFailed': { zh: '探测失败', en: 'Probe failed' },
  'settings.networkError': { zh: '网络错误', en: 'Network error' },
  'settings.textModels': { zh: '个文本', en: 'text' },
  'settings.thinkingModels': { zh: '个推理', en: 'thinking' },
  'settings.visionModels': { zh: '个视觉', en: 'vision' },
  'settings.cheapModels': { zh: '个低价', en: 'cheap' },
  'settings.igModels': { zh: '个图片生成', en: 'image gen' },
  'settings.embeddingModels': { zh: '个嵌入', en: 'embedding' },
  'settings.discovered': { zh: '发现', en: 'Discovered' },
  'settings.balanceDetected': { zh: '已检测到余额接口', en: 'balance endpoint detected' },
  'settings.thinkingFormatSuggested': { zh: '建议思维格式', en: 'suggested thinking format' },
  'settings.configSaved': { zh: '服务器配置已保存，设置已实时生效。', en: 'Server config saved. Settings applied in real-time.' },
  'settings.saved': { zh: '✅ 已保存', en: '✅ Saved' },
  'settings.serverConfigFailed': { zh: '⚠️ 无法加载服务器配置', en: '⚠️ Cannot load server config' },

  // ══════════════════════════════════════
  //  Browser Bridge Modal
  // ══════════════════════════════════════
  'browser.title': { zh: '浏览器桥接', en: 'Browser Bridge' },
  'browser.desc': { zh: '通过 Chrome 扩展让 AI 读取和交互你的浏览器标签页。', en: 'Use Chrome extension to let AI read and interact with your browser tabs.' },
  'browser.checking': { zh: '正在检查...', en: 'Checking...' },
  'browser.downloadExtension': { zh: '下载扩展程序', en: 'Download Extension' },
  'browser.downloadDesc': { zh: '点击下方按钮下载 ZIP 文件，然后解压。', en: 'Click the button below to download the ZIP file, then extract it.' },
  'browser.downloadZip': { zh: '下载扩展 ZIP', en: 'Download Extension ZIP' },
  'browser.installInChrome': { zh: '在 Chrome 中安装', en: 'Install in Chrome' },
  'browser.installDesc': { zh: '打开 chrome://extensions/ → 启用开发者模式 → 点击加载已解压的扩展程序 → 选择解压后的 browser_extension 文件夹。', en: 'Open chrome://extensions/ → Enable Developer mode → Click Load unpacked → Select the extracted browser_extension folder.' },
  'browser.verify': { zh: '验证连接', en: 'Verify Connection' },
  'browser.verifyDesc': { zh: '点击工具栏中的扩展图标，应显示已连接。然后在此处开启浏览器功能。', en: 'Click the extension icon in the toolbar — it should show Connected. Then enable browser features here.' },
  'browser.aiFeatures': { zh: '浏览器桥接的 AI 功能', en: 'Browser Bridge AI Features' },
  'browser.listTabs': { zh: '列出所有打开的标签页（标题、URL）', en: 'List all open tabs (title, URL)' },
  'browser.readTab': { zh: '读取任意标签页的文本内容，或使用 CSS 选择器', en: 'Read text content of any tab, or use CSS selectors' },
  'browser.executeJs': { zh: '在任意标签页中执行 JavaScript（点击、填充表单、提取数据）', en: 'Execute JavaScript in any tab (click, fill forms, extract data)' },
  'browser.close': { zh: '关闭', en: 'Close' },
  'browser.enable': { zh: '启用浏览器桥接', en: 'Enable Browser Bridge' },

  // ══════════════════════════════════════
  //  Memory Modal
  // ══════════════════════════════════════
  'memory.title': { zh: '记忆积累 · AI 自动学习并应用的知识库', en: 'Memory · AI auto-learns and applies knowledge' },
  'memory.all': { zh: '全部', en: 'All' },
  'memory.project': { zh: '项目', en: 'Project' },
  'memory.global': { zh: '全局', en: 'Global' },
  'memory.searchPh': { zh: '搜索记忆…', en: 'Search memories…' },
  'memory.emptyTitle': { zh: '还没有积累任何记忆', en: 'No memories accumulated yet' },
  'memory.emptyHint': { zh: 'AI 在对话中发现有用模式时会自动保存记忆\n你也可以点击下方「+ 新建」手动添加', en: 'AI auto-saves memories when it discovers useful patterns\nYou can also click "+ New" below to add manually' },
  'memory.createNew': { zh: '创建新记忆', en: 'Create New Memory' },
  'memory.namePh': { zh: '记忆名称 (短横线命名, e.g. react-hooks-convention)', en: 'Memory name (kebab-case, e.g. react-hooks-convention)' },
  'memory.descPh': { zh: '简短描述 — 什么时候该使用这条记忆？', en: 'Brief description — when should this memory be used?' },
  'memory.bodyPh': { zh: '记忆内容（支持 Markdown）…', en: 'Memory content (Markdown supported)…' },
  'memory.projectScope': { zh: '项目级', en: 'Project scope' },
  'memory.globalScope': { zh: '全局', en: 'Global' },
  'memory.tagsPh': { zh: '标签（逗号分隔）', en: 'Tags (comma separated)' },
  'memory.create': { zh: '创建', en: 'Create' },
  'memory.new': { zh: '新建', en: 'New' },
  'memory.installSkill': { zh: '安装技能包', en: 'Install skill' },
  'memory.openSkillsStore': { zh: '技能市场', en: 'Skills Store' },
  'memory.dropTitle': { zh: '松开鼠标以安装技能包', en: 'Drop to install skill package' },
  'memory.dropHint': { zh: '支持 Claude Skills / OpenClaw / AgentSkills 格式的 .zip', en: 'Accepts Claude Skills / OpenClaw / AgentSkills .zip bundles' },
  'memory.enableMemory': { zh: '启用 Memory', en: 'Enable Memory' },

  // ══════════════════════════════════════
  //  Mobile Sheet
  // ══════════════════════════════════════
  'mobile.options': { zh: '选项', en: 'Options' },
  'mobile.thinkingDepth': { zh: '思考深度', en: 'Thinking Depth' },
  'mobile.off': { zh: '关闭', en: 'Off' },
  'mobile.medium': { zh: '中', en: 'Med' },
  'mobile.high': { zh: '高', en: 'High' },
  'mobile.xhigh': { zh: '超高', en: 'xHigh' },
  'mobile.max': { zh: '最大', en: 'Max' },
  'mobile.aiEnhance': { zh: 'AI 增强', en: 'AI Enhance' },
  'mobile.memoryInject': { zh: '记忆注入', en: 'Memory Inject' },
  'mobile.memoryInjectDesc': { zh: '注入累积经验', en: 'Inject accumulated experience' },
  'mobile.autoTranslate': { zh: '自动翻译', en: 'Auto Translate' },
  'mobile.autoTranslateDesc': { zh: '中英互译', en: 'CN↔EN translate' },
  'mobile.tools': { zh: '工具', en: 'Tools' },
  'mobile.projectAssistant': { zh: '项目助手', en: 'Project Assistant' },
  'mobile.projectAssistantDesc': { zh: '打开项目面板', en: 'Open project panel' },
  'mobile.aiCanAsk': { zh: 'AI 可向你提问', en: 'AI can ask you' },
  'mobile.backend': { zh: '后端', en: 'Backend' },
  'mobile.allFeatures': { zh: '全部功能', en: 'All features' },
  'mobile.checkingStatus': { zh: '检查中...', en: 'Checking...' },
  'mobile.modes': { zh: '模式', en: 'Modes' },
  'mobile.parallelAgents': {
    zh: '并行子代理 · 无需 Project',
    en: 'Parallel agents · no Project needed',
  },
  'mobile.autoExecLoop': { zh: '自主执行循环', en: 'Auto-exec loop' },
  'mobile.paperReader': { zh: '论文阅读', en: 'Paper Reader' },
  'mobile.paperReaderDesc': { zh: 'PDF阅读 + 问答 + 报告 + Babel PDF', en: 'PDF reading + Q&A + reports + Babel PDF' },

  // ══════════════════════════════════════
  //  MyDay
  // ══════════════════════════════════════
  'myday.title': { zh: '我的一天', en: 'My Day' },
  'myday.refresh': { zh: '生成/刷新报告', en: 'Generate/refresh report' },
  'myday.done': { zh: '完成', en: 'Done' },
  'myday.open': { zh: '未完成', en: 'Open' },
  // Date helpers
  'myday.today': { zh: '今天', en: 'Today' },
  'myday.yesterday': { zh: '昨天', en: 'Yesterday' },
  'myday.weekdays': { zh: '日,一,二,三,四,五,六', en: 'Sun,Mon,Tue,Wed,Thu,Fri,Sat' },
  'myday.weekdayPrefix': { zh: '周', en: '' },
  'myday.monthDay': { zh: '{m}月{d}日', en: '{m}/{d}' },
  'myday.yearMonth': { zh: '{y}年{m}月', en: '{y}/{m}' },
  'myday.dateFull': { zh: '{y}年{m}月{d}日', en: '{m}/{d}/{y}' },
  // Calendar day headers
  'myday.calWeek': { zh: '日,一,二,三,四,五,六', en: 'S,M,T,W,T,F,S' },
  // Status labels
  'myday.statusDone': { zh: '✓ 完成', en: '✓ Done' },
  'myday.statusInProgress': { zh: '⏳ 进行中', en: '⏳ In Progress' },
  'myday.statusBlocked': { zh: '⛔ 受阻', en: '⛔ Blocked' },
  'myday.statusIncomplete': { zh: '进行中', en: 'In Progress' },
  'myday.toggleStatus': { zh: '切换状态', en: 'Toggle status' },
  // Section labels
  'myday.todayTodos': { zh: '今日待办', en: "Today's TODOs" },
  'myday.unfinishedSection': { zh: '未完成', en: 'Unfinished' },
  'myday.activeSection': { zh: '进行中', en: 'In Progress' },
  'myday.doneSection': { zh: '已完成', en: 'Completed' },
  'myday.tomorrowPlan': { zh: '明日计划', en: "Tomorrow's Plan" },
  'myday.nextDayPlan': { zh: '次日计划', en: 'Next Day Plan' },
  'myday.todoItems': { zh: '待办事项', en: 'TODOs' },
  // Stream info
  'myday.convCount': { zh: '{n} 个对话', en: '{n} conversations' },
  'myday.independentConvs': { zh: '{n} 个独立对话', en: '{n} independent conversations' },
  // Waiting / empty
  'myday.reportNotGenerated': { zh: '报告尚未生成', en: 'Report not generated yet' },
  'myday.checkingConvs': { zh: '正在查询对话数量…', en: 'Checking conversation count…' },
  'myday.hasConvsHint': { zh: '有 {n} 个对话，点击上方刷新按钮或下方按钮生成报告', en: '{n} conversations found — click refresh or the button below to generate' },
  'myday.noConvsHint': { zh: '还没有对话记录，开始聊天后可以生成报告', en: 'No conversations yet — start chatting to generate a report' },
  'myday.generateBtn': { zh: '生成报告', en: 'Generate Report' },
  'myday.generateDaily': { zh: '生成日报', en: 'Generate Report' },
  'myday.quietDay': { zh: '这天很安静', en: 'A quiet day' },
  'myday.noConvsFound': { zh: '没有找到对话记录', en: 'No conversations found' },
  // Progress stages
  'myday.generating': { zh: '正在生成报告', en: 'Generating report' },
  'myday.stageStarting': { zh: '正在启动…', en: 'Starting…' },
  'myday.stageExtracting': { zh: '扫描对话', en: 'Scanning conversations' },
  'myday.stageAnalyzing': { zh: 'AI 分析', en: 'AI Analysis' },
  'myday.stageSaving': { zh: '保存报告', en: 'Saving report' },
  'myday.stageScanMsg': { zh: '扫描对话 {c}/{t}', en: 'Scanning {c}/{t}' },
  'myday.stageAnalyzeMsg': { zh: 'LLM 分析 {n} 个对话…', en: 'Analyzing {n} conversations…' },
  'myday.stageSaveMsg': { zh: '保存报告…', en: 'Saving report…' },
  'myday.genHint': { zh: '你可以切换到其他日期，生成不会中断', en: 'You can switch dates — generation continues in background' },
  'myday.genFailed': { zh: '生成失败', en: 'Generation failed' },
  'myday.genFailRetry': { zh: '启动生成失败，请重试', en: 'Failed to start generation, please retry' },
  'myday.analyzing': { zh: '分析中…', en: 'Analyzing…' },
  // Stats
  'myday.convStat': { zh: '{n} 对话', en: '{n} convs' },
  'myday.streamStat': { zh: '{n} 工作流', en: '{n} streams' },
  // Badges
  'myday.badgeYesterday': { zh: '昨日', en: 'Yesterday' },
  'myday.badgeCarried': { zh: '延续', en: 'Carried' },
  // TODO actions
  'myday.addPlaceholder': { zh: '添加待办…', en: 'Add a task…' },
  'myday.markDone': { zh: '标记完成', en: 'Mark done' },
  'myday.markUndone': { zh: '标记未完成', en: 'Mark undone' },
  'myday.startConv': { zh: '开始对话', en: 'Start conversation' },
  'myday.deleteTodo': { zh: '删除', en: 'Delete' },
  // Inherited prompt
  'myday.hasConvsToday': { zh: '今日已有 {n} 个对话', en: '{n} conversations today' },
  // Close button
  'myday.close': { zh: '关闭', en: 'Close' },
  // Misc streams
  'myday.miscQA': { zh: '零碎问答', en: 'Misc Q&A' },
  // Reminder toast
  'myday.reminderTitle': { zh: '📋 查看今日日报', en: '📋 Check your daily report' },
  'myday.reminderBody': { zh: '今天有 {n} 个对话，来看看你的工作总结吧', en: "You had {n} conversations today — review your summary" },
  'myday.reminderBodyGeneric': { zh: '来看看今天的工作总结吧', en: 'Review your daily work summary' },

  // ══════════════════════════════════════
  //  Conversation Actions
  // ══════════════════════════════════════
  'conv.copyFailed': { zh: '复制失败', en: 'Copy failed' },
  'conv.cannotLoadOriginal': { zh: '无法加载原始对话内容', en: 'Cannot load original conversation content' },
  'conv.copying': { zh: '对话复制中…', en: 'Copying conversation…' },
  'conv.copied': { zh: '对话已复制 ✓', en: 'Conversation copied ✓' },
  'conv.copy': { zh: '副本', en: 'Copy' },
  'conv.messages': { zh: '条对话', en: 'messages' },
  'conv.quote': { zh: '引用', en: 'Quote' },
  'conv.quoteConv': { zh: '引用对话', en: 'Quote conversation' },
  'conv.branch': { zh: '分支', en: 'Branch' },
  'conv.reply': { zh: '引用', en: 'Quote' },

  // ══════════════════════════════════════
  //  Folders
  // ══════════════════════════════════════
  'folder.moveToFolder': { zh: '移入文件夹', en: 'Move to folder' },
  'folder.removeFromFolder': { zh: '移出文件夹', en: 'Remove from folder' },
  'folder.newFolder': { zh: '新建文件夹', en: 'New Folder' },
  'folder.movedToFolder': { zh: '已移入文件夹', en: 'Moved to folder' },
  'folder.removedFromFolder': { zh: '已移出文件夹', en: 'Removed from folder' },
  'folder.createTitle': { zh: '新建文件夹', en: 'New Folder' },
  'folder.namePh': { zh: '文件夹名称', en: 'Folder name' },
  'folder.cancel': { zh: '取消', en: 'Cancel' },
  'folder.create': { zh: '创建', en: 'Create' },
  'folder.creating': { zh: '创建中…', en: 'Creating…' },
  'folder.createFailed': { zh: '创建失败', en: 'Create failed' },
  'folder.cannotCreate': { zh: '无法创建文件夹', en: 'Cannot create folder' },
  'folder.created': { zh: '文件夹已创建', en: 'Folder created' },
  'folder.renameTitle': { zh: '重命名文件夹', en: 'Rename Folder' },
  'folder.ok': { zh: '确定', en: 'OK' },
  'folder.deleteTitle': { zh: '删除文件夹', en: 'Delete Folder' },
  'folder.deleteConfirm': { zh: '确定删除文件夹', en: 'Delete folder' },
  'folder.deleteHint': { zh: '文件夹内的对话不会被删除，只是变为未分类。', en: 'Conversations in the folder will not be deleted, just become uncategorized.' },
  'folder.deleted': { zh: '文件夹已删除', en: 'Folder deleted' },
  'folder.rename': { zh: '重命名', en: 'Rename' },
  'folder.deleteAction': { zh: '删除文件夹', en: 'Delete folder' },

  // ══════════════════════════════════════
  //  Translation
  // ══════════════════════════════════════
  'translate.failed': { zh: '翻译失败，点击重试', en: 'Translation failed, click to retry' },
  'translate.translatingToCN': { zh: '正在翻译为中文…', en: 'Translating to Chinese…' },
  'translate.original': { zh: '原文', en: 'Original' },
  'translate.translated': { zh: '译文', en: 'Translation' },
  // Retry/status sub-messages shown below the spinner when the backend reports
  // a transient issue (rate-limit, empty output, etc.) while retrying.
  'translate.retry.started': { zh: '正在调用翻译模型，请稍候…', en: 'Translating, please wait…' },
  'translate.retry.in_progress': { zh: '翻译仍在进行中…', en: 'Still translating…' },
  'translate.retry.rate_limited': { zh: '所有密钥被限流，正在重试…', en: 'All keys rate-limited, retrying…' },
  'translate.retry.dispatch_error': { zh: '接口错误，正在重试…', en: 'Provider error, retrying…' },
  'translate.retry.dispatch_failed_final': { zh: '接口错误重试已耗尽', en: 'Provider errors exhausted' },
  'translate.retry.empty_output': { zh: '返回为空，正在换模型重试…', en: 'Empty response, retrying with another model…' },
  'translate.retry.empty_final': { zh: '多次返回为空', en: 'Empty response after retries' },
  'translate.retry.truncated': { zh: '输出被截断，正在重试…', en: 'Output truncated, retrying…' },
  'translate.retry.truncated_final': { zh: '多次截断后返回部分结果', en: 'Output truncated after retries' },
  'translate.retry.mt_fallback': { zh: '机器翻译失败，已切换到大模型', en: 'MT provider failed, using LLM' },
  'translate.retry.timed_out': { zh: '翻译超时，已直接发送原文', en: 'Translation timed out, sent original text' },

  // ══════════════════════════════════════
  //  Time / Relative
  // ══════════════════════════════════════
  'time.secondsAgo': { zh: 's前', en: 's ago' },
  'time.minutesAgo': { zh: 'm前', en: 'm ago' },
  'time.hoursAgo': { zh: 'h前', en: 'h ago' },
  'time.daysAgo': { zh: 'd前', en: 'd ago' },
  'time.justNow': { zh: '刚刚', en: 'Just now' },
  'time.minutesAgoFull': { zh: '分钟前', en: 'min ago' },
  'time.hoursAgoFull': { zh: '小时前', en: 'hr ago' },
  'time.daysAgoFull': { zh: '天前', en: 'd ago' },

  // ══════════════════════════════════════
  //  Message Actions / Status
  // ══════════════════════════════════════
  'msg.contentFiltered': { zh: '内容违反安全政策，已被模型安全系统拦截', en: 'Content violated safety policy and was blocked' },
  'msg.prematureClose': { zh: 'API网关超时，模型深度思考中被中断。内容可能不完整。', en: 'API gateway timeout, model was interrupted during deep thinking. Content may be incomplete.' },
  'msg.gatewayInterrupt': { zh: '网关中断', en: 'Gateway interrupt' },
  'msg.abnormalStop': { zh: 'API流异常终止（连接被代理/网关中断，缺失finish标记）。回复内容可能不完整。', en: 'API stream terminated abnormally (connection interrupted by proxy/gateway, missing finish marker). Response may be incomplete.' },
  'msg.abnormalInterrupt': { zh: '异常中断', en: 'Abnormal interrupt' },
  'msg.thinking': { zh: '思', en: 'think' },
  'msg.rounds': { zh: '轮', en: 'rnd' },
  'msg.tooManyModels': { zh: '模型太多？在 <b>设置 → 模型</b> 中隐藏不需要的模型', en: 'Too many models? Hide unwanted ones in <b>Settings → Display</b>' },

  // ══════════════════════════════════════
  //  Queue
  // ══════════════════════════════════════
  'queue.messagesQueued': { zh: '条消息排队中', en: 'messages queued' },
  'queue.clearAll': { zh: '全部清空', en: 'Clear all' },
  'queue.images': { zh: '张图片', en: 'images' },
  'queue.attachment': { zh: '附件', en: 'Attachment' },
  'queue.cancelMsg': { zh: '取消此消息', en: 'Cancel this message' },

  // ══════════════════════════════════════
  //  Agent backends
  // ══════════════════════════════════════
  'agent.notInstalled': { zh: '未安装', en: 'Not installed' },
  'agent.notAuthenticated': { zh: '未认证', en: 'Not authenticated' },
  'agent.ready': { zh: '就绪', en: 'Ready' },

  // ══════════════════════════════════════
  //  Log Clean
  // ══════════════════════════════════════
  'logClean.detected': { zh: '检测到日志噪音，可节省', en: 'Log noise detected, can save' },
  'logClean.chars': { zh: '字符', en: 'characters' },

  // ══════════════════════════════════════
  //  Conversation reference
  // ══════════════════════════════════════
  'convRef.title': { zh: '@ 引用对话', en: '@ Reference Conversation' },
  'convRef.searchPh': { zh: '搜索对话标题…', en: 'Search conversation titles…' },
  'convRef.noMatch': { zh: '没有匹配的对话', en: 'No matching conversations' },
  'convRef.noOther': { zh: '暂无其他对话', en: 'No other conversations' },
  'convRef.messages': { zh: '条消息', en: 'messages' },
  'convRef.cannotRef': { zh: '无法引用当前对话', en: 'Cannot reference current conversation' },
  'convRef.alreadyRef': { zh: '该对话已在引用列表中', en: 'This conversation is already referenced' },
  'convRef.referenced': { zh: '已引用', en: 'Referenced' },
  'convRef.removeRef': { zh: '移除引用', en: 'Remove reference' },
  'convRef.convRef': { zh: '对话引用', en: 'Conversation reference' },

  // ══════════════════════════════════════
  //  Batch / Multi-gen
  // ══════════════════════════════════════
  'batch.allModels': { zh: '全模型', en: 'All models' },
  'batch.multiGen': { zh: '连抽', en: 'multi-gen' },
  'batch.success': { zh: '成功', en: 'success' },

  // ══════════════════════════════════════
  //  Daily Optimizer panel / badge
  // ══════════════════════════════════════
  'optimizer.badgeTitle': { zh: '每日优化器 — 自主改进建议', en: 'Daily Optimizer — autonomous improvement proposals' },
  'optimizer.panelTitle': { zh: '🧭 每日优化器', en: '🧭 Daily Optimizer' },
  'optimizer.loading': { zh: '加载中…', en: 'Loading…' },
  'optimizer.disabled': { zh: '每日优化器已禁用。可在 设置 → 功能模块 中启用。', en: 'Daily Optimizer is disabled. Enable it in Settings → Feature Modules.' },
  'optimizer.empty': { zh: '每日优化器还没有提出任何建议。它每晚 03:30 本地时间自动运行，分析日志并可能提出或自动应用小改进。', en: "The Daily Optimizer hasn't proposed anything yet. It wakes up nightly at 03:30 local, analyses the day's logs, and may suggest or auto-apply small improvements." },
  'optimizer.runNow': { zh: '▶ 立即运行', en: '▶ Run now' },
  'optimizer.runNowTitle': { zh: '同步运行优化器', en: 'Synchronously run the optimiser now' },
  'optimizer.running': { zh: '⏳ 运行中…', en: '⏳ Running…' },
  'optimizer.lastRefresh': { zh: '最近刷新：', en: 'Last refresh: ' },
  'optimizer.appliedToday': { zh: '✅ 今日已应用', en: '✅ Applied today' },
  'optimizer.pendingReview': { zh: '🟡 等待你审阅', en: '🟡 Pending your review' },
  'optimizer.revertedToday': { zh: '↩️ 今日已撤销/过期', en: '↩️ Reverted / expired today' },
  'optimizer.olderProposals': { zh: '📜 更早的建议', en: '📜 Older proposals' },
  'optimizer.approve': { zh: '✓ 批准', en: '✓ Approve' },
  'optimizer.approveTitle': { zh: '立即应用此建议', en: 'Apply this proposal now' },
  'optimizer.reject': { zh: '✗ 拒绝', en: '✗ Reject' },
  'optimizer.rejectTitle': { zh: '标记为已拒绝', en: 'Mark as rejected' },
  'optimizer.rejectPrompt': { zh: '拒绝此建议的理由（可选）：', en: 'Reason for rejecting this proposal (optional):' },
  'optimizer.revert': { zh: '↩ 撤销', en: '↩ Revert' },
  'optimizer.revertTitle': { zh: '撤销此次变更', en: 'Undo this change' },
  'optimizer.revertConfirm': { zh: '撤销此次已应用的变更？底层配置将回滚，lib.SKIP_DOMAINS（或类似）会热重载。', en: 'Revert this applied change? The underlying config will be rolled back and lib.SKIP_DOMAINS (or similar) will be hot-reloaded.' },
  'optimizer.blockSearchDomain': { zh: '🚫 屏蔽搜索域名', en: '🚫 block search domain' },
  'optimizer.untitled': { zh: '(无标题)', en: '(untitled)' },
  // Status reasons emitted by the backend (lib/optimizer/applier.py).
  // _optStatusReasonText() in optimizer.js maps the raw English string
  // to one of these keys; unknown reasons fall through to the raw text.
  'optimizer.reason.notInWhitelist': { zh: '未在自动应用白名单中', en: 'not in auto-apply whitelist' },
  'optimizer.reason.dryRun': { zh: '试运行（未实际应用）', en: 'dry run (not actually applied)' },
  'optimizer.reason.unknownAction': { zh: '未知的操作类型：{type}', en: 'unknown action type: {type}' },
  'optimizer.reason.manualApproveBlocked': { zh: '手动批准被拦截：操作类型不在自动应用白名单中', en: 'manual approve blocked: action_type not in auto-apply whitelist' },

  // ══════════════════════════════════════
  //  Settings — Search/Fetch params (inline labels)
  // ══════════════════════════════════════
  'settings.fetchTopNFull': { zh: '抓取前 N 条 <span style="color:var(--text-tertiary);font-weight:normal">（搜索后自动抓取排名靠前的网页）</span>', en: 'Fetch Top N <span style="color:var(--text-tertiary);font-weight:normal">(auto-fetch top-ranked pages after search)</span>' },
  'settings.fetchTimeoutFull': { zh: '抓取超时 <span style="color:var(--text-tertiary);font-weight:normal">（秒）</span>', en: 'Fetch Timeout <span style="color:var(--text-tertiary);font-weight:normal">(seconds)</span>' },
  'settings.maxCharsSearchFull': { zh: '最大字符数 <span style="color:var(--text-tertiary);font-weight:normal">（搜索结果页面）</span>', en: 'Max Characters <span style="color:var(--text-tertiary);font-weight:normal">(search result pages)</span>' },
  'settings.maxCharsDirectFull': { zh: '最大字符数 <span style="color:var(--text-tertiary);font-weight:normal">（直接抓取 URL）</span>', en: 'Max Characters <span style="color:var(--text-tertiary);font-weight:normal">(direct URL fetch)</span>' },
  'settings.maxCharsPdfFull': { zh: '最大字符数 <span style="color:var(--text-tertiary);font-weight:normal">（PDF 文件，0=不限制）</span>', en: 'Max Characters <span style="color:var(--text-tertiary);font-weight:normal">(PDF files, 0=unlimited)</span>' },
  'settings.maxBytesFull': { zh: '最大下载大小 <span style="color:var(--text-tertiary);font-weight:normal">（字节，默认 20MB）</span>', en: 'Max Download Size <span style="color:var(--text-tertiary);font-weight:normal">(bytes, default 20MB)</span>' },
  'settings.maxCharsPdfPh': { zh: '0=不限制', en: '0=unlimited' },
  'settings.bypassDomainsFull': { zh: '绕过域名 <span style="color:var(--text-tertiary);font-weight:normal">（每行一个，后缀匹配 — 例如 <code>.your-corp.com</code>）</span>', en: 'Bypass Domains <span style="color:var(--text-tertiary);font-weight:normal">(one per line, suffix matching — e.g. <code>.your-corp.com</code>)</span>' },
  'settings.fallbackModelFull': { zh: '回退模型 <span style="color:var(--text-tertiary);font-weight:normal">（主模型失败时自动切换）</span>', en: 'Fallback Model <span style="color:var(--text-tertiary);font-weight:normal">(auto-switch on primary failure)</span>' },
  'settings.defaultModelFull': { zh: '默认模型 <span style="color:var(--text-tertiary);font-weight:normal">（LLM_MODEL）</span>', en: 'Default Model <span style="color:var(--text-tertiary);font-weight:normal">(LLM_MODEL)</span>' },
  'settings.allowedUsersFull': { zh: '允许的用户 <span style="color:var(--text-tertiary);font-weight:normal">（飞书 open_id，每行一个 — 留空表示允许所有人）</span>', en: 'Allowed Users <span style="color:var(--text-tertiary);font-weight:normal">(Feishu open_id, one per line — empty = allow everyone)</span>' },

  // Feishu app ID/secret placeholders
  'settings.feishuAppIdPh': { zh: 'cli_xxxx（从 open.feishu.cn 获取）', en: 'cli_xxxx (get from open.feishu.cn)' },
  'settings.feishuAppSecretPh': { zh: '留空则保持不变', en: 'Leave empty to keep unchanged' },
  'settings.feishuHowto': { zh: '在 <a href="https://open.feishu.cn/app" target="_blank" style="color:var(--accent-color)">open.feishu.cn</a> 创建飞书应用 → 启用机器人能力 → 在上方填写 <strong>App ID</strong> 和 <strong>App Secret</strong>。凭证修改需要重启服务器才能生效。', en: 'Create a Feishu app at <a href="https://open.feishu.cn/app" target="_blank" style="color:var(--accent-color)">open.feishu.cn</a> → enable bot capability → fill in <strong>App ID</strong> and <strong>App Secret</strong> above. Credential changes require a server restart.' },

  // OAuth extended strings
  'settings.oauthPageDesc': { zh: '使用 ChatGPT Plus / Claude Pro 订阅账号登录，无需 API Key，直接使用订阅额度。', en: 'Log in with ChatGPT Plus / Claude Pro — no API Key needed, use your subscription quota directly.' },
  'settings.oauthChinaNote': { zh: '⚠️ 中国用户需要全程代理（Clash/VPN），授权弹窗和服务器换 token 都需要能访问外网。建议在<strong>本地浏览器无痕窗口</strong>中完成授权。', en: '⚠️ Users in China need a proxy (Clash/VPN) throughout. Both the auth popup and server-side token exchange require internet access. Use a <strong>local incognito window</strong> to authorize.' },
  'settings.oauthPopupBlocked': { zh: '如弹窗无法打开，请复制链接到<strong>开了代理的浏览器无痕窗口</strong>中打开：', en: 'If the popup is blocked, copy the link into a <strong>proxy-enabled incognito browser window</strong>:' },
  'settings.oauthCopyLink': { zh: '复制链接', en: 'Copy Link' },
  'settings.oauthCopied': { zh: '✓ 已复制', en: '✓ Copied' },
  'settings.oauthClaudeCodeHint': { zh: '授权成功后页面显示授权码，复制 <strong>code#state</strong> 粘贴到下方：', en: 'After authorization succeeds, the page shows an auth code — copy the <strong>code#state</strong> and paste below:' },
  'settings.oauthCodexCbHint': { zh: '授权成功后，复制浏览器地址栏中的回调 URL 粘贴到下方：', en: 'After authorization, copy the callback URL from the browser address bar and paste below:' },
  'settings.oauthClaudePh': { zh: '粘贴 code#state（或完整回调 URL）', en: 'Paste code#state (or full callback URL)' },
  'settings.oauthCodexPh': { zh: '粘贴回调 URL (http://localhost:1455/auth/callback?code=...)', en: 'Paste callback URL (http://localhost:1455/auth/callback?code=...)' },
  'settings.oauthSubmit': { zh: '提交', en: 'Submit' },
  'settings.oauthClaudeDesc': { zh: '登录 Claude 订阅，使用 Sonnet / Opus 等模型，无需 API Key。', en: 'Log in with your Claude subscription to use Sonnet / Opus — no API Key required.' },
  'settings.oauthCodexDesc': { zh: '登录 ChatGPT 订阅，使用 Codex 模型，请求自动转换为 Responses API 格式。', en: 'Log in with your ChatGPT subscription to use Codex — requests auto-converted to Responses API format.' },
  'settings.oauthNote1': { zh: '点击登录 → 弹窗打开官方授权页 → 用订阅账号登录并授权', en: 'Click Login → popup opens the official auth page → log in with your subscription account and authorize' },
  'settings.oauthNote2': { zh: '授权后 Claude 显示 <code>code#state</code>、ChatGPT 自动回调，按提示操作即可', en: 'After authorization Claude shows <code>code#state</code>; ChatGPT auto-callbacks — follow the on-screen hints' },
  'settings.oauthNote3': { zh: '如弹窗被拦截，点击「复制链接」到<strong>本地浏览器无痕窗口</strong>中打开（避免登录态冲突）', en: 'If the popup is blocked, click "Copy Link" and open it in a <strong>local incognito window</strong> (avoids session conflicts)' },
  'settings.oauthNote4': { zh: 'Token 过期后自动刷新，无需重新登录', en: 'Tokens auto-refresh on expiry — no need to re-login' },

  // Network proxy: extended body/hint
  'settings.httpProxyBody': { zh: '配置用于所有出站请求（LLM API、网页搜索、页面抓取）的 HTTP/HTTPS 代理。留空则使用系统环境变量（<code>http_proxy</code> / <code>https_proxy</code>）。修改立即生效，无需重启。', en: 'Configure HTTP/HTTPS proxy for all outbound requests (LLM API, web search, page fetch). Leave empty to use system env vars (<code>http_proxy</code> / <code>https_proxy</code>). Changes take effect immediately.' },
  'settings.proxyBypassBody': { zh: '在此添加不需要走代理的域名后缀或主机名（每行一个，后缀匹配）。匹配的请求会<strong>完全绕过 HTTP 代理</strong>。', en: 'Add domain suffixes or hostnames that should bypass the proxy (one per line, suffix matching). Matching requests <strong>fully bypass the HTTP proxy</strong>.' },
  'settings.proxyBypassTipFull': { zh: '<strong>💡 提示：</strong>内网地址和 LLM API 域名都应加在这里。企业/VPN 代理会静默断开长连接（SSE 流），导致 <code>BrokenPipeError</code>，添加对应域名即可解决。也可通过环境变量 <code>PROXY_BYPASS_DOMAINS</code>（逗号分隔）配置，两处合并生效。', en: '<strong>💡 Tip:</strong> Internal addresses and LLM API domains should both be added here. Corporate/VPN proxies silently drop long-lived connections (SSE streams) causing <code>BrokenPipeError</code> — adding the domain here fixes it. You can also set <code>PROXY_BYPASS_DOMAINS</code> (comma-separated) env var; both merge.' },

  // ══════════════════════════════════════
  //  MCP manual-add form (settings)
  // ══════════════════════════════════════
  'mcp.manualAddSummary': { zh: '⚙ 手动添加自定义服务器', en: '⚙ Manually add a custom server' },
  'mcp.fieldName': { zh: '名称', en: 'Name' },
  'mcp.fieldTransport': { zh: '传输协议', en: 'Transport' },
  'mcp.transportStdio': { zh: 'stdio (本地命令)', en: 'stdio (local command)' },
  'mcp.transportSse': { zh: 'SSE (远程 URL)', en: 'SSE (remote URL)' },
  'mcp.fieldCommand': { zh: '命令', en: 'Command' },
  'mcp.fieldArgs': { zh: '参数', en: 'Arguments' },
  'mcp.fieldArgsHint': { zh: '（每行一个）', en: '(one per line)' },
  'mcp.fieldEnv': { zh: '环境变量', en: 'Environment Variables' },
  'mcp.fieldEnvHint': { zh: '（每行 KEY=VALUE）', en: '(KEY=VALUE per line)' },
  'mcp.fieldDesc': { zh: '描述', en: 'Description' },
  'mcp.fieldDescHint': { zh: '（可选）', en: '(optional)' },
  'mcp.saveConnect': { zh: '保存并连接', en: 'Save & Connect' },
  'mcp.installConnect': { zh: '安装并连接', en: 'Install & Connect' },
  'mcp.cancel': { zh: '取消', en: 'Cancel' },

  // ══════════════════════════════════════
  //  Browser bridge modal
  // ══════════════════════════════════════
  'browser.stepDownload': { zh: '下载扩展程序', en: 'Download extension' },
  'browser.stepDownloadDesc': { zh: '点击下方按钮下载 ZIP 文件，然后解压。', en: 'Click the button below to download the ZIP file, then extract it.' },
  'browser.stepDownloadBtn': { zh: '下载扩展 ZIP', en: 'Download Extension ZIP' },
  'browser.stepInstall': { zh: '在 Chrome 中安装', en: 'Install in Chrome' },
  'browser.stepVerify': { zh: '验证连接', en: 'Verify connection' },
  'browser.stepVerifyDesc': { zh: '点击工具栏中的扩展图标，应显示 <strong>已连接</strong>。然后在此处开启浏览器功能。', en: 'Click the extension icon in the toolbar — it should show <strong>Connected</strong>. Then turn on the browser feature here.' },
  'browser.capsTitle': { zh: '浏览器桥接的 AI 功能', en: 'Browser bridge AI capabilities' },
  'browser.capListTabs': { zh: '列出所有打开的标签页（标题、URL）', en: 'List all open tabs (title, URL)' },
  'browser.capReadTab': { zh: '读取任意标签页的文本内容，或使用 CSS 选择器', en: 'Read text content of any tab, or use CSS selectors' },
  'browser.capExecJs': { zh: '在任意标签页中执行 JavaScript（点击、填充表单、提取数据）', en: 'Run JavaScript in any tab (click, fill forms, extract data)' },
  'browser.checkingDots': { zh: '正在检查...', en: 'Checking…' },

  // ══════════════════════════════════════
  //  Debug / toolbar tooltips
  // ══════════════════════════════════════
  'debug.copyAllTooltip': { zh: '复制全部', en: 'Copy all' },
  'toolbar.aiEnhanceTooltip': { zh: 'AI 增强', en: 'AI Enhance' },
  'toolbar.externalToolsTooltip': { zh: '外部工具', en: 'External Tools' },
  'toolbar.execModeTooltip': { zh: '执行模式', en: 'Execution Mode' },
  'toolbar.moreOptionsTooltip': { zh: '更多选项', en: 'More options' },
  'toolbar.exitCreativeTooltip': { zh: '退出创作模式 (Esc)', en: 'Exit creative mode (Esc)' },
  'ig.stdResTooltip': { zh: '1024px · 标准分辨率', en: '1024px · Standard resolution' },
  'ig.hdResTooltip': { zh: '2048px · 高清分辨率', en: '2048px · HD resolution' },
  'ig.generateTooltip': { zh: '生成 (Enter)', en: 'Generate (Enter)' },
  'feishu.statusDotTitle': { zh: '连接状态', en: 'Connection status' },
  'settings.loadingPricing': { zh: '正在加载价格数据...', en: 'Loading pricing data…' },
  'settings.feishuBotTitleSuffix': { zh: '飞书 (Lark) 机器人', en: 'Feishu (Lark) Bot' },
  'browser.stepInstallDesc': { zh: '打开 <code class="copyable-url" data-tooltip="点击复制" style="cursor:pointer;position:relative;border-bottom:1px dashed var(--accent-color)" onclick="_safeClipboardWrite(\'chrome://extensions/\').then(()=>{this.classList.add(\'copied\')}).catch(()=>{})">chrome://extensions/</code> → 启用 <strong>开发者模式</strong> → 点击 <strong>加载已解压的扩展程序</strong> → 选择解压后的 <code>browser_extension</code> 文件夹。', en: 'Open <code class="copyable-url" data-tooltip="Click to copy" style="cursor:pointer;position:relative;border-bottom:1px dashed var(--accent-color)" onclick="_safeClipboardWrite(\'chrome://extensions/\').then(()=>{this.classList.add(\'copied\')}).catch(()=>{})">chrome://extensions/</code> → enable <strong>Developer mode</strong> → click <strong>Load unpacked</strong> → pick the extracted <code>browser_extension</code> folder.' },

  // ══════════════════════════════════════
  //  Common
  // ══════════════════════════════════════
  'common.confirm': { zh: '确定', en: 'OK' },
  'common.cancel': { zh: '取消', en: 'Cancel' },
  'common.close': { zh: '关闭', en: 'Close' },
  'common.save': { zh: '保存', en: 'Save' },
  'common.delete': { zh: '删除', en: 'Delete' },
  'common.edit': { zh: '编辑', en: 'Edit' },
  'common.loading': { zh: '正在加载…', en: 'Loading…' },
  'common.error': { zh: '错误', en: 'Error' },
  'common.success': { zh: '成功', en: 'Success' },
  'common.required': { zh: '必填', en: 'Required' },
  'common.officialApi': { zh: '官方 API', en: 'Official API' },
  'common.relayApi': { zh: '中转 API', en: 'Relay API' },
};

/**
 * Get translated text for a key. Falls back to the key itself if not found.
 * Supports interpolation: t('key', { count: 5 }) replaces {count} in the string.
 *
 * @param {string} key - Translation key
 * @param {Object} [params] - Optional interpolation parameters
 * @returns {string} Translated text
 */
function t(key, params) {
  var entry = _i18n[key];
  var text = entry ? (entry[_i18nLang] || entry.zh || key) : key;
  if (params) {
    for (var k in params) {
      if (params.hasOwnProperty(k)) {
        text = text.replace(new RegExp('\\{' + k + '\\}', 'g'), params[k]);
      }
    }
  }
  return text;
}

/**
 * Set the UI language and re-apply all translations.
 * @param {'zh'|'en'} lang
 */
function setLanguage(lang) {
  if (lang !== 'zh' && lang !== 'en') return;
  _i18nLang = lang;
  localStorage.setItem('tofu_ui_lang', lang);
  _applyI18n();
}

/**
 * Apply translations to all elements with data-i18n attributes.
 * Also handles data-i18n-placeholder, data-i18n-title.
 */
function _applyI18n() {
  // Text content
  document.querySelectorAll('[data-i18n]').forEach(function(el) {
    var key = el.getAttribute('data-i18n');
    if (key) el.textContent = t(key);
  });
  // innerHTML (for entries that contain HTML tags)
  document.querySelectorAll('[data-i18n-html]').forEach(function(el) {
    var key = el.getAttribute('data-i18n-html');
    if (key) el.innerHTML = t(key);
  });
  // Placeholder
  document.querySelectorAll('[data-i18n-placeholder]').forEach(function(el) {
    var key = el.getAttribute('data-i18n-placeholder');
    if (key) el.placeholder = t(key);
  });
  // Title (tooltip)
  document.querySelectorAll('[data-i18n-title]').forEach(function(el) {
    var key = el.getAttribute('data-i18n-title');
    if (key) el.title = t(key);
  });
  // Update html lang attribute
  document.documentElement.lang = _i18nLang === 'zh' ? 'zh-CN' : 'en';
  // Sync language dropdown if it exists
  var langSelect = document.getElementById('settingLanguage');
  if (langSelect) langSelect.value = _i18nLang;
  // Sync language picker cards
  _syncLangPicker(_i18nLang);
}

/**
 * Handler for language dropdown change in settings.
 * @param {'zh'|'en'} lang
 */
function _onLanguageChange(lang) {
  setLanguage(lang);
  _syncLangPicker(lang);
  // Re-render dynamic content that uses t()
  if (typeof renderConversationList === 'function') renderConversationList();
  // ★ Repaint the open conversation so message chrome (tool labels,
  //   finish-info, timestamps) re-renders with the new language. The
  //   former call was to renderMessages(), which never existed — the
  //   whole-chat repaint is renderChat(conv). (caught by tsc --checkJs)
  if (typeof renderChat === 'function' && typeof getActiveConv === 'function') {
    var _activeConv = getActiveConv();
    if (_activeConv) renderChat(_activeConv, true);
  }
  if (typeof _refreshOptimizerPanel === 'function') {
    try { _refreshOptimizerPanel(); } catch (e) { /* panel may not be open */ }
  }
  if (typeof _renderProvidersTab === 'function') {
    try { _renderProvidersTab(); } catch (e) { /* tab may not be initialised */ }
  }
  if (typeof _renderPresetsTab === 'function' && typeof _serverConfig !== 'undefined' && _serverConfig) {
    try { _renderPresetsTab(_serverConfig); } catch (e) { /* tab may not be initialised */ }
  }
}

/** Sync visual language picker cards to the given lang */
function _syncLangPicker(lang) {
  document.querySelectorAll('.lang-option').forEach(function(el) {
    el.classList.toggle('active', el.getAttribute('data-lang') === lang);
  });
}

// Apply translations once DOM is ready
document.addEventListener('DOMContentLoaded', function() {
  _applyI18n();
});
