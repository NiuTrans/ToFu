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
  'toolbar.swarmAgentsDesc': { zh: '并行子代理分解任务', en: 'Parallel sub-agents decompose tasks' },
  'toolbar.autonomousMode': { zh: '自主模式', en: 'Autonomous Mode' },
  'toolbar.autonomousModeDesc': { zh: '自主执行+自我审查循环', en: 'Autonomous execution + self-review loop' },
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
  'settings.tabAdvanced': { zh: '高级', en: 'Advanced' },
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
  'settings.imageMaxWidthPh': { zh: '0=不压缩', en: '0=no compression' },
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
  'settings.keepToolHistory': { zh: '保留工具调用历史', en: 'Keep Tool Call History' },
  'settings.keepToolHistoryDesc': { zh: '多轮对话时保留完整的工具调用记录（搜索内容、网页抓取结果等），模型能看到之前搜过什么，避免重复调用。关闭可节省 token 但模型会丢失工具上下文', en: 'Preserve full tool call records (search results, fetched pages, etc.) across conversation turns. Model can see what was searched before, avoiding redundant calls. Disable to save tokens but model loses tool context' },

  // ══════════════════════════════════════
  //  Settings — Providers Tab
  // ══════════════════════════════════════
  'settings.providersTitle': { zh: 'API 服务商 & 模型', en: 'API Providers & Models' },
  'settings.autoSetup': { zh: '🚀 自动配置', en: '🚀 Auto Setup' },
  'settings.fromTemplate': { zh: '⚡ 从模板添加', en: '⚡ From Template' },
  'settings.syncTemplate': { zh: '同步模板', en: 'Sync Template' },
  'settings.customProvider': { zh: '+ 自定义服务商', en: '+ Custom Provider' },
  'settings.providersDesc': { zh: '使用「🚀 自动配置」只需填写 API 地址和密钥，系统自动发现模型、检测余额接口和定价。也可从模板添加或手动创建。', en: 'Use "🚀 Auto Setup" — just enter API URL and key, the system auto-discovers models, balance endpoint, and pricing. You can also add from templates or create manually.' },
  'settings.loadingConfig': { zh: '正在加载配置…', en: 'Loading config…' },
  'settings.loadingFailed': { zh: '加载服务器配置失败。请检查服务器是否正在运行。', en: 'Failed to load server config. Please check if the server is running.' },
  'settings.noProviders': { zh: '还没有配置服务商。点击"+ 自定义服务商"开始添加。', en: 'No providers configured. Click "+ Custom Provider" to start.' },
  'settings.keys': { zh: '个密钥', en: 'keys' },
  'settings.models': { zh: '个模型', en: 'models' },
  'settings.disabled': { zh: '已禁用', en: 'Disabled' },
  'settings.displayName': { zh: '显示名称', en: 'Display Name' },
  'settings.baseUrl': { zh: 'API 地址 (Base URL)', en: 'API URL (Base URL)' },
  'settings.apiKeys': { zh: 'API 密钥', en: 'API Keys' },
  'settings.apiKeysHint': { zh: '每行一个，安全存储', en: 'One per line, securely stored' },
  'settings.balanceUrl': { zh: '余额查询地址', en: 'Balance Query URL' },
  'settings.balanceUrlHint': { zh: '可选 — OpenAI 兼容的账单接口', en: 'Optional — OpenAI-compatible billing endpoint' },
  'settings.checkBalance': { zh: '查询 ▸', en: 'Check ▸' },
  'settings.modelsPath': { zh: '模型发现路径', en: 'Models Discovery Path' },
  'settings.modelsPathHint': { zh: '可选 — 默认在 Base URL 后追加 /models', en: 'Optional — defaults to Base URL + /models' },
  'settings.customHeaders': { zh: '自定义请求头', en: 'Custom Headers' },
  'settings.customHeadersHint': { zh: '可选 — JSON 对象，如 {"X-My-Header": "value"}', en: 'Optional — JSON object, e.g. {"X-My-Header": "value"}' },
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
  'mobile.parallelAgents': { zh: '并行子代理', en: 'Parallel agents' },
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
  if (typeof renderMessages === 'function') renderMessages();
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
