# 论文阅读体验设计稿(Paper Reading Experience)——从「一份文档」到「一位陪读导师」

> 状态:**P0–P4 全量落地**(2026-08-01 落笔;owner 2026-08-02 一键批「批准全量 P0-P4」;
> 2026-08-02 五期全部交付)。
>
> | 期 | 状态 | commit | 证据 |
> |---|---|---|---|
> | P0 获得感+成本可见 | ✅ | `fe270ce9` | insight 四级链默认 ON + anchor 确定性解析 + secondPasses 进 finish tag + 锚定卡/回收卡;后端 12/12 + 前端 25 检查,回归 61 |
> | P1 启发 | ✅ | `2df7408f` | provocation→`_paperAskQuestion` 开辩、open_problem→`_startResearchJob` 提案;JSDOM +10 |
> | P2 易懂 | ✅ | `5b8f97d0` | checkpoint_engine(节锚自测卡)+ 术语类比列 + 零 LLM 速览折叠;后端 9/9 + JSDOM +17 |
> | P3 深度 | ✅ | `3209c43e` | deepen_engine(qa 机架克隆,三模式)+ `deep:` 缓存槽(section_hash 新鲜度)+ 首个骑通用 poll 工厂的 paper 任务;后端 6/6 + JSDOM +9 |
> | P4 沉浸 | ✅ | `7865fa34` | paper_notes 表(schema 45)+ CRUD + 页边批注 + 专注模式 + 会话小结 + **xp store 根修**(_reportView 新字面量跨实例丢 payload);后端 3/3 + JSDOM +18 |
>
> 触发诉求(owner 原话):「优化阅读模式的报告,通过机制让读者**获得洞见、感到启发、
> 内容有深度、容易理解、毫不费力地沉浸深思**。前后端怎么设计可以脑暴。」
> 脑暴轮结论:报告本身的质量不是瓶颈,瓶颈是它是**静态交付物**;五个目标描述的
> 是「被引导的阅读过程」。本稿把脑暴落成机制、接口、存储与分期验收。

---

## 0. 结论先行

1. **方向不是把报告写得更长更好,而是把「已建成的引擎群」接进阅读流。** insight
   二遍(A/B 验证过增益)默认关着;ideate 点子机(1639 行)没有单论文入口;QA
   划词问句已建成但只在选中文本时触发。本稿的大部分工作是**接线**,不是新建。
2. **体验数据与报告正文严格分离**:报告本体(已验证的忠实度产物)一个字节不动,
   所有体验增量走独立持久化键(`insight:`/`termfill:` 已趟出的复合 lang 键模式),
   可独立重算、独立失败、独立删除。
3. **深度走「按需深挖」,不走 `:::deep` 围栏**(§3.1 两条路线显式对比后的裁定):
   主 prompt 零改动,深度成本只在读者真的点开时支付。
4. **成本可见性是 P0 的一部分,不是附加项**:insight 默认开 = 每篇报告固定多一次
   rubric 评分 + 可能的带工具合成,这笔账必须进 finish tag 让读者看见(§3.3)。
5. **一切默认开/关的治理**:交互面默认开(owner 2026-08-06:「默认开启就行，
   不需要设计成开关」——Settings 开关当批退役);headless/BYO
   调用面维持 fail-closed 默认关(对齐 `personal_scope` 既有先例,§3.4)。

打个比方:现在的报告是**印刷精美的教科书**,本稿要把它变成**坐在旁边的导师**——
在你读到某处时指一句「注意这里」,问「刚才那节真懂了吗」,把深度调到你的水平,
把这篇论文和你上周读过的那篇连起来。导师的大脑(insight/QA/ideate 引擎)已经在
仓库里了,本稿设计的是把大脑接进阅读流的**神经**。

---

## 1. 现状体检(全部盘上核实,2026-08-01)

### 1.1 已建成且在线的

| 机制 | 位置 | 状态 |
|---|---|---|
| 忠实度报告(复现级方法/设计抉择链/实证复现清单) | `lib/paper/prompts.py` + `report_engine/` | ✅ 主力,质量基线 |
| 引用幻觉审计 + 术语自洽审计(零 LLM 闸) | `lib/paper/citation_audit.py` / `terminology_audit/` | ✅ 卡片进 meta,前端渲染 |
| 缓存报告重开合并 insight | `routes/paper.py:186` `_append_cached_insight` | ✅ **已在读路径**(脑暴轮误判为缺口,实为已建;P0 的增量只有「锚定分发+默认开」) |
| 报告内划词 → 引用 → 自动发问 QA | `static/js/paper/qa.js` `_askAboutPaperSelection` | ✅ 已在线 |
| 前端阅读基建:目录栏+滚动间谍、callout、术语悬停卡、阅读时长条(自适应语速)、位置记忆、审计卡、finish tag | `static/js/paper/report.js`(3646 行)+ `reader_prefs.js` | ✅ 已在线 |

### 1.2 已建成但**没接进阅读流**的(本稿的主要原料)

| 资产 | 位置 | 断点 |
|---|---|---|
| **Insight 二遍引擎**(赌注/迁移连接/观点/开放问题/挑衅追问;arXiv grounding;A/B 验证 baseline≤4.0 时显著增益) | `lib/paper/insight_engine/` | `insight_enabled()` 只读 `TOFU_PAPER_INSIGHT` env,**默认 OFF**;产出整块 appended 文末,无节锚 |
| **Termfill 术语回填** | `lib/paper/terminology_backfill.py` | 同样默认 OFF(`personal_scope` 门 + 全局 kill switch) |
| **ideate 点子机**(新颖性闸 + 反 A+B) | `lib/paper/ideate.py` 1639 行 | **无单论文路由**——routes 里 grep 不到 `ideate`;只挂在 auto-research 管线(harvest→survey→ideate,`lib/research/recipe.py:81`)里,离阅读面隔着整个 research tab |
| QA agentic 问答(带工具、报告+原文上下文) | `lib/paper/qa_engine.py` | 只从 QA tab 输入框/划词进入;insight 的挑衅追问没有一键送进去的通道 |

### 1.3 真缺口(脑暴轮没抓到或抓错的)

| 缺口 | 证据 | 后果 |
|---|---|---|
| **二遍成本不进 finish tag** | `report_engine/__init__.py` 的 `_usage_total` 只累计报告本体轮次;insight/termfill 在自己的引擎里 dispatch,用量不落 meta | 一旦默认开,每篇报告的真实成本对读者**不可见**——违背「低成本」目标里最要紧的「账要诚实」 |
| **meta 持久化先于二遍** | `_run_report_task` 先 `_build_report_meta` + upsert,**之后**才跑 `_maybe_run_insight` | 二遍结果(用量、baseline、锚)无处落;要设计 meta 二次持久化(§4.3) |
| **insight 无节锚** | `insight_prompts.py` 的 JSON schema 没有 anchor 字段;`_render.py` 整段渲染 | 只能堆文末,多数读者读不到 |
| **报告按语言独立生成,体验数据无键位纪律** | `paper_reports.lang` 已有 `en/zh/review:…/insight:<ui>/termfill:<ui>` | 新增 checkpoint/deepen 键必须跟同一纪律,否则双语各算一遍成本翻倍(§3.5) |

---

## 2. 五个目标 → 机制总表

| 目标 | 机制 | 复用什么 | 分期 |
|---|---|---|---|
| **获得感** | insight 默认开(带用户开关)+ 节锚分发卡 + 文库连接可点卡 + 读完回收卡 | insight_engine 全套 + `_append_cached_insight` 读路径 | P0 |
| **启发** | 挑衅追问 → 一键送 QA 开辩;开放问题 → 一键进 ideate/research | qa_engine(已有)+ research 管线(已有) | P1 |
| **易懂** | 随堂检查卡(每节自测)+ 术语类比行 + 速览模式(零 LLM 折叠) | 新 checkpoint 二遍(单发无工具)+ 术语表 prompt 加一列 + 前端确定性折叠 | P2 |
| **深度** | 节级「再深一层」+ 公式逐步推导(按需、按节、按语言缓存) | **克隆 qa_engine 任务机架**(agent_loop/事件流/runtime) | P3 |
| **沉浸** | 专注模式 + 页边批注(新 `paper_notes` 表)+ 节边界思考提示 + 会话小结 | 前端为主;批注走新表 CRUD;思考提示复用 P0 锚定卡 | P4 |

贯穿机制:**成本可见性**(所有二遍/按需用量进 finish tag 分解)随 P0 落地。

---

## 3. 关键决策(含否决项与证据)

### 3.1 深度分层:`:::deep` 围栏 vs 零 LLM 后切分 + 按需深挖 —— **裁定后者**

| 维度 | A:`:::deep` 围栏塞进主 prompt | B:确定性小节切分 + 按需深挖(裁定) |
|---|---|---|
| 主 prompt 风险 | 改已验证的忠实度 prompt;深度内容要穿过 preamble strip / 图片注入 / renderMarkdown 全管道,**模型不守格式是本仓库反复实证的**(`report_engine` 注释:「LLMs ignore format rules … so the guarantee lives here」),还得再写一道确定性兜底去校验/剥离坏围栏 | **主 prompt 零改动**;切分是对成品 markdown 的确定性解析(h2/h3 列表),不可能漂移 |
| 成本形状 | 每个读者都为深潜内容付 token,**不管他点不点开**;单遍 max_tokens 已顶到 128k 天花板,深度内容直接挤占正文预算 | 深度成本只在读者真的点击时支付;不点 = 零成本。对齐「低成本」目标 |
| 深度可演化 | 深度档次烤死在生成时刻,改档=重生成整篇 | 深挖是独立任务:同一节可反复深挖(deeper/derive/ELI5),按节缓存,不触碰报告本体 |
| 机架 | 要新写围栏校验/渲染/降级 | **克隆 qa_engine 机架**(agent_loop + 事件流 + runtime + 前端 poll/push 双通道),几乎没有新机械 |
| 速度感 | 读完即全有(但生成更慢) | 点击后 ~10-30s 任务往返(有进度卡 + push 通道,体感与 QA 一致) |

**裁定:B。** 屋内的既定风格就是「prompt 要求 + 确定性兜底」(`finalize_review_body`、
图片注入、术语审计全是确定性后处理);A 路线反而把更多信任押给 prompt。
「速览档」不靠 A 也能有:前端对成品做**确定性折叠**(每节只留首段 + callout 块),
零 LLM、零风险(见 §5 `reading_xp.js`)。

### 3.2 `anchor_section` 确定性解析(prompt 提名,代码定址)

insight JSON 的每条 connection/provocation 增加可选 `anchor` 字段(模型填**它认为
相关的节标题原文**)。模型提名**不可信**,落地必须确定性:

```
解析链(零 LLM,lib/paper/insight_engine/_anchors.py 新模块):
1. 规范化(大小写/空白/emoji 剥离)后的精确匹配报告 h2/h3 列表
2. 失败 → token 重叠率 ≥ 0.6 的模糊匹配(取最高分,平票取靠前者)
3. 再失败 → anchor = None(回退现行行为:进文末块)
```

- 解析结果**随 insight 行持久化**(写进该行的 meta JSON),读路径直接消费,
  不重复解析——报告编辑/重新生成后锚不失效也不漂移;
- 跨语言:insight 本来就按 `insight:<ui_lang>` 分语言生成,锚在各自语言的正文上
  解析,天然不跨语言漂移;
- 这正是 `finalize_review_body` 先例的第三次应用:**prompt 负责提名,代码负责定址**。

### 3.3 成本可见性(owner 钦定 P0 第一优先)

**现状账**:insight 默认开后,每篇报告固定多 1 次 rubric 评分(~3k tokens 出),
闸门触发时多 1 轮带工具合成(最多 6 轮工具 + 合成)。今天这笔账**不落任何 meta**。

**设计**:

1. 每个二遍引擎在返回 dict 里新增 `usage` 字段(各引擎已有 out-dict 模式,
   在各自的 dispatch 回调里累计,与报告引擎 `_accumulate_usage` 同款);
2. `_maybe_run_insight` / `_maybe_run_termfill` / 未来的 checkpoint 钩子把 usage +
   关键判决(baseline/fired/closed)合并进 `report_meta['secondPasses']`:
   ```json
   "secondPasses": {
     "insight": {"fired": true, "baseline": 3.4, "promptTokens": 18230,
                  "completionTokens": 2400, "costCny": 0.041},
     "checkpoints": {"promptTokens": 9100, "completionTokens": 800, "costCny": 0.012}
   }
   ```
3. **meta 二次持久化**:二遍跑完后 upsert 同一 `paper_reports` 行(**只更新 meta
   字段,正文不动**),并向 live 读者发 `report_meta` 事件,finish tag 热更新;
4. 前端 finish tag:总成本 = 报告本体 + Σ secondPasses,徽章悬停出分解 tooltip
   (「报告 ¥0.31 + 洞察 ¥0.04 + 检查点 ¥0.01」)。

** NEUTER 判据**:摘掉 secondPasses 合并,finish tag 总额必须精确回落到本体量——
测试证明合并是真接线而非装饰。

### 3.4 开关治理:headless fail-closed(Settings 开关已退役 2026-08-06)

```
interactive(操作者界面):  env TOFU_PAPER_INSIGHT 总闸 > 默认 ON(永远默认开)
headless / BYO:           默认 OFF(fail-closed),须显式 cfg 开启
```

- **用户级开关已退役**:owner 2026-08-06 指令「这两个默认开启就行，不需要设计成
  开关」——`settings_panels/general.html` 的两枚 toggle、其前端接线与
  server_config `paper.reading_experience` 读写分支当批全删;引擎侧
  `insight_enabled` / `checkpoints_enabled` 收敛为三级链(cfg 显式戳 > env > 默认
  ON)。已存的 `paper.reading_experience` 配置节从此不被读取(无效，无副作用)。
  费用可关诉求由两处吸收:报告底部 finish tag 的费用分解(§3.3,每一遍花多少
  透明可见)与 headless 面 cfg 门(headless 仍然 fail-closed)。
- headless 面:`personal_scope` 注册表的 `paperInsightEnabled` /
  `paperCheckpointsEnabled` capability 不动,每个 headless cfg-builder 显式
  stamp,未 stamp = False;
- **rubric 闸门不受开关影响,始终生效**——开关管「要不要这个功能」,闸门管
  「这篇报告需不需要补」(baseline>4.0 的报告自动只花一次评分钱)。

### 3.5 多语言不翻倍:`<kind>:<ui_lang>` 键位纪律

| 产物 | 持久化键 | 生成时机 |
|---|---|---|
| 报告本体 | `en` / `zh`(现行) | 用户点生成该语言 |
| insight | `insight:<ui_lang>`(现行) | 该语言报告完成后,二遍钩子 |
| termfill | `termfill:<ui_lang>`(现行) | 同上 |
| checkpoints | `checkpoints:<ui_lang>`(新) | 同上,**只随实际生成的语言** |
| 深挖缓存 | `deep:<mode>:<sec>:<ui_lang>`(新) | 读者点击「再深一层」时 |
| 批注 | `paper_notes` 表(新,按 paper_hash+lang) | 读者写批注时 |

**纪律:体验产物只为「用户真的生成了的那个语言」生产。** 报告本体已是按语言
按需生成(点哪语言生成哪语言),所有二遍钩子挂在报告任务尾部,天然继承这一纪律;
深挖/批注由读者行为驱动,不产生沉默成本。

---

## 4. 后端设计:体验清单(experience manifest)

报告任务完成后,除正文外,后端围绕同一 `paper_hash` 维护一族**独立键**的产物——
合称体验清单。正文一个字节不动;每个产物独立生成、独立失败、独立缓存、独立删除。

### 4.1 新增/修改的后端件

| 件 | 动作 | 说明 |
|---|---|---|
| `insight_engine/_anchors.py` | **新** | §3.2 确定性锚解析 |
| `insight_prompts.py` | 改 | JSON schema 加可选 `anchor` 字段(提名节标题) |
| `insight_engine/_run.py` | 改 | grounding 后跑锚解析,锚写入 insight dict;返回加 `usage` |
| `report_engine/_hooks.py` | 改 | 合并 secondPasses usage;meta 二次持久化;发 `report_meta` 事件 |
| `report_engine/__init__.py` | 改 | `done` 事件后、hooks 后重 upsert meta(正文不动) |
| `insight_engine/_config.py` | 改 | `insight_enabled()` 改三级解析(§3.4);personal_scope 注册新 capability |
| `lib/paper/checkpoint_engine.py` | **新(P2)** | 单发无工具 JSON 调用:输入成品报告 → `[{section, question, answer}]`;复用 insight 的 JSON 修复模式(temp 0.45 + temp-0 修复重问);复用 `_persist_insight` 写路径落 `checkpoints:<ui_lang>` |
| `lib/paper/deepen_engine.py` | **新(P3)** | **克隆 qa_engine**:同 agent_loop/事件/runtime;prompt 换深挖指令;输入=节正文+报告全文+原文上下文(qa_context 复用);mode ∈ `deeper/derive/eli5`;结果落 `deep:<mode>:<sec>:<ui_lang>` |
| `routes/api_v1/paper.py` | 改 | 新端点(全部 `api_ok` 信封,§4.2) |
| `lib/database/_core_schema.py` | 改(P4) | 新表 `PAPER_NOTES`(唯一表定义源,PG/SQLite 双语 DDL 自动同源) |
| `lib/agent_core/personal_scope.py` | 改 | 注册 `paperInsightEnabled` / `paperCheckpointsEnabled` capability |

### 4.2 新路由清单(全部 api_ok 信封,过 `test_api_contract_drift` 棘轮)

| 路由 | 方法 | 说明 |
|---|---|---|
| `/api/v1/paper/deepen/start` | POST | `{paper_hash, section_idx, mode, lang}` → task_id;命中 `deep:…` 缓存直接返回成品(标 `cached: true`) |
| `/api/v1/paper/deepen/poll/<task_id>` | GET | 走 `_task_routes.register_task_routes` 通用工厂(与 qa 同款) |
| `/api/v1/paper/notes` | GET/POST | 按 `paper_hash+lang` 列出 / 新建批注 |
| `/api/v1/paper/notes/<id>` | PATCH/DELETE | 改 / 删 |

批注锚:`{heading_idx, char_offset, quote}` 三元组——heading_idx+offset 定址,
quote 兜底(报告重新生成后按 quote 模糊重锚,失败则标「孤儿批注」仍可见)。

### 4.3 meta 二次持久化时序(修 §1.3 的「meta 先于二遍」)

```
报告流完成 → _build_report_meta → upsert 行 → done 事件(现行,不变)
  → insight 钩子 → 合并 secondPasses.insight
  → checkpoints 钩子(P2) → 合并 secondPasses.checkpoints
  → upsert 同一行(仅 meta 字段) → 发 report_meta 事件(前端徽章热更新)
```

约束:二次 upsert **只写 meta 列**;任一二遍失败只记 warning 且不阻断后续钩子
(现行 hooks 的全包裹纪律推广)。

---

## 5. 前端设计:四个新模块,report.js 一行不长

`static/js/paper/report.js` 已 3646 行,**冻结不再加功能**。新功能开新文件
(window-scope var 模式,`_DEFERRED_FILES` 登记,`lib/js_bundler.py` `_BUNDLE_FILES`
钉静态清单,i18n 走 `paper.*` 命名空间):

| 新模块 | 职责 | 关键机制 |
|---|---|---|
| `paper/reading_xp.js` | 体验轨:insight 锚定卡(按锚插到对应节标题后)+ 文库连接可点卡 + 节末检查翻卡(P2)+ 读完回收卡 + **速览模式**(零 LLM:每节留首段+要点 callout+图,折叠其余;无内容节保底留首块) | 锚卡数据来自 insight 行 meta;回收卡在滚动进度 100% 时显现,内容=thesis+3 要点+连接+1 开放问题;速览折叠是纯 DOM 操作 |
| `paper/deepen.js` | 节标题旁「再深一层」钮 + 公式块「逐步推导」钮 + 结果抽屉 | start/poll 走 §4.2 路由 + `paperAttachPush` 双通道(qa.js 同款);抽屉缓存在内存,重开报告从 `deep:` 键读 |
| `paper/notes.js` | 选中→「记一笔」popover→页边标记;批注面板列表;「就这条批注问 AI」送 QA | 复用 `_handlePaperTextSelection` 的选区捕获(report 分支已存在);CRUD 走 §4.2 |
| `paper/focus_mode.js` | 专注模式:非当前段降透明度,j/k 逐段移动,Esc 退出 | 纯 DOM/CSS;与阅读位置记忆共存(段索引即锚) |

**移动端**:锚定卡/检查卡/批注一律**内嵌行内**(节标题后/节末),不做右侧栏——
右栏在窄屏必死,行内卡天然响应式。TOC 侧栏维持现状。

**复用不新造**:
- 挑衅追问 → QA 开辩 = 调 `_switchPaperTab('qa')` + 预填输入框 + `_sendPaperQuestion()`
  (qa.js 已有全部件,只差一个 `_paperAskQuestion(text)` 公开入口);
- 开放问题 → ideate = 跳 research tab 并把问题文本预填为 direction(研究管线已有
  direction→harvest→survey→ideate 全链,`static/js/paper/research.js`);
- 文库连接卡点击 = 已有 library 打开流程(`paper/library.js`)。

---

## 6. 分期实施与验收(每期 failing-first + NEUTER,提交即验证)

> 测试纪律(全期通用):`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`;每批后
> `--collect-only` 闸;新套件先写红(failing-first)再实现;NEUTER(摘除接线)
> 必须精确咬回对应红且 cmp 逐字节还原;前端 JSDOM harness 对齐既有 paper 套件。

### P0 — 获得感 + 成本可见性(最高价值,纯接线)

| 交付 | 验收测试 |
|---|---|
| insight 三级开关解析(§3.4)+ Settings 阅读卡 | 单测:cfg 显式>env>默认 ON;headless 未 stamp=False;NEUTER 摘掉 cfg 层 → 解析回退 env |
| insight JSON `anchor` 字段 + `_anchors.py` 确定性解析 + 锚随行持久化 | 单测:精确/模糊/回退三路径;跨语言各自解析;NEUTER 摘掉模糊层 → 模糊用例精确回红 |
| 前端锚定卡 + 文库连接可点卡 + 读完回收卡 | JSDOM:卡插到正确节后;连接卡点击调 library 打开;100% 进度出回收卡;NEUTER 摘锚 → 卡回文末 |
| secondPasses 用量合并 + meta 二次持久化 + finish tag 分解 | 单测:meta 含 secondPasses 且总额=本体+二遍;二次 upsert 只动 meta 列;NEUTER 摘合并 → 总额精确回落 |

### P1 — 启发:念头有出口

| 交付 | 验收测试 |
|---|---|
| `_paperAskQuestion(text)` 公开入口 + 挑衅追问卡「开辩」钮 | JSDOM:点击→tab 切换+预填+发送;NEUTER 摘入口 → 钮失效 |
| 开放问题卡「展开成提案」→ research tab 预填 direction | JSDOM:预填内容与开放问题文本一致 |

### P2 — 易懂

| 交付 | 验收测试 |
|---|---|
| `checkpoint_engine.py` + `checkpoints:<ui>` 持久化 + 节末翻卡 | 单测:JSON 修复模式;重开报告读路径合并(对齐 `_append_cached_insight` 模式);NEUTER 摘持久化 → 重开无卡 |
| 术语类比列(prompt 加列 + 悬停卡渲染类比行) | 单测:术语表解析出类比列;JSDOM:悬停卡含类比行;术语审计兼容 |
| 速览模式(零 LLM 折叠) | JSDOM:折叠后每节=首段+callout;切回完整 = 字节还原 DOM |

### P3 — 深度按需

| 交付 | 验收测试 |
|---|---|
| `deepen_engine.py`(qa 机架克隆)+ start/poll 路由 + `deep:` 缓存 | 单测:缓存命中不再跑 LLM;三种 mode 各落各键;api_ok 信封;NEUTER 摘缓存 → 同参重跑 |
| 前端节钮 + 公式推导钮 + 抽屉 | JSDOM:抽屉渲染/push 事件应用/缓存键正确 |

### P4 — 沉浸

| 交付 | 验收测试 |
|---|---|
| `paper_notes` 表 + CRUD 路由 + 前端批注 | 单测:锚三元组写入/重锚/孤儿标;JSDOM:选区→popover→标记→重开还原 |
| 专注模式 + 会话小结 | JSDOM:j/k 移动段索引正确;小结含时长/节数/批注数 |

### 顺序理由

P0 全是接线已建成资产,获得感/启发立竿见影且**成本可见性必须赶在默认开之前或同时**
(否则有一段「成本不可见的默认开」窗口);P1 不接新引擎;P2/P3 才引入新二遍/新任务型;
P4 纯增量(新表)收尾。每期独立可交付、独立可回滚(体验键独立,删键即回滚)。

---

## 7. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 默认开后单篇成本上升 | rubric 固定 + 闸门触发时带工具合成 | §3.3 成本可见 + §3.4 用户可关 + rubric 闸门拦住 insight 饱和的报告;owner 审 P0 时看 finish tag 分解实测 |
| 锚解析误锚 | 模糊匹配把卡插错节 | 阈值 0.6 + 平票靠前 + 失败回退文末(宁缺毋错);锚随行持久化,可观测 |
| 检查点质量问题 | 单发无工具调用,问题可能平庸 | 复用 insight JSON 修复模式;P2 先做小样本目检再放量;用户可关 |
| 深挖成本失控 | 每节每 mode 一次带工具任务 | 按节缓存;深挖走 rubric 式轻模型档位(设置里可选);用量进 secondPasses |
| 批注重生成漂移 | 报告重生成后锚失效 | quote 兜底模糊重锚;失败标孤儿不丢弃 |
| 共享树并发 | 多 sibling 并行改同一 HEAD | 每期一个 commit、精确 pathspec、失败先行+NEUTER(项目既有纪律);P0 不碰 `routes/paper.py` 读路径以外的兄弟热区,**避开 pt_931e16c4(api-contract)在改的 paper.py 47 站点迁移面**——新增端点全部落在 `routes/api_v1/paper.py` 且 api_ok 原生,不与迁移批冲突 |

---

## 8. 与既有设计稿的关系

- `docs/PAPER_MEDIA_UX_DESIGN.md` / `PAPER_PODCAST_DESIGN.md`:媒体面(播客/视频)
  的权威记录;本稿不动媒体面,播客/视频 tab 的既有接线复用。
- `docs/AUTO_RESEARCH_SYSTEM_DESIGN.md`:research 管线(harvest→survey→ideate);
  本稿 P1 的「开放问题→提案」是它的入口预填,不是它的修改。
- `docs/PRODUCTION_PIPELINE_DESIGN.md`:深挖任务**不骑**产出底盘——它是交互式
  快任务(QA 家族),不是「一句话→成品」长任务;若未来深挖演化为批量预生成,
  再评估是否过 `lib/production/`。
- `docs/API_CONTRACT.md`:全部新路由 api_ok 原生,棘轮只降不升。
