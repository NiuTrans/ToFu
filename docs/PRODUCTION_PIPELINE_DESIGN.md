# Tofu 产出底盘设计稿(Production Substrate)——「一句话 → 成品」的通用承载形态

> 状态:**P4 / P5 / P6 / P7 全部落地**。分期完成。
> (2026-07-25 落笔;2026-07-26 更新实施状态 —— P7 实测结论见 §9,P6 抑取范围由它决定)
>
> | 期 | 状态 | 证据 |
> |---|---|---|
> | P4 视频前半段(主题→调研→文案→真时间轴) | ✅ 已落地 | `a6f45f0c` — `lib/motion_video/_recipe.py` + `produce_video` + 崩溃续跑;套件 17/17 |
> | P5 每镜子 agent 画面 | ✅ 已落地 | `a98f3c12` — `lib/motion_video/_scene_author.py`;套件 16/16(双 NEUTER) |
> | P6 阶段图契约平移 | ✅ 已落地 | `8578dcb5` — `lib/production/stages.py`(`git mv`,字节相同);守卫 7/7 |
> | P6 剩余(ProductionRuntime + 清单/重投) | ✅ 已落地 | `424d9c28` — `lib/production/runtime.py` + `jobs.py`;三个能力全部骑上,门面名不变;套件 22/22 |
> | P6 `deliverable` / 进度双投影 / artifacts binary | ⏸ **刻意不抽** | §9.3:第三个样本没用上 → 是视频/播客共性而非全局共性 |
> | P6 `_registries()` 发现制 | ✅ 已落地 | `0c768268` — motion / podcast 对通用任务 API 从此可见;套件 5/5（failing-first） |
> | P7 第三配方验证 | ✅ 已落地 | `lib/longform/` — 512 行（目标 ≤600）;套件 9/9 含 NEUTER;实测结论见 §9 |
> | P8 PPT/视频内容内核统一 | ✅ 已落地 | `lib/production/research.py` + `contracts.py`;三车道证据、当前事实闸、叙事/素材/finding 契约共用，媒介渲染分离 |
> | P9 视频镜头 IR / 配方目录 | ✅ 已落地 | `lib/motion_video/_shot_recipes.py`;13 个 renderer-neutral 配方、语义选型/运动族与能量审计、配方化四锚点 QA、资产 ready 闸、API/UI 可观测 |
>
> 触发诉求(owner 原话):「我希望有一天,我只要在输入框里说我想要一个关于某新闻话题的
> 科普知识视频,视频就被创作出来了,用户甚至不需要感知」+「我不确定现在这个技能形态是否合适」。
> 前置:`docs/MOTION_VIDEO_DESIGN.md`(渲染机械层,P0–P3 已交付)。
> 本稿**不是** motion video 的续集,而是它的上位抽象 + 它缺失的前半段。

---

## 0. 结论先行

1. **不该做成「视频增强」**——「一句话 → 成品」是一整类能力(视频/播客/PPT/长报告/海报),
   不是视频的一个补丁。
2. **不该做成技能包**——技能包规范没有生命周期/依赖/产物契约(§3 外部证据),
   承载不了长任务。技能包应退为**编导手册**(风格、动效规则、分镜策略)。
3. **应该做成三层**:**① 产出底盘(横向复用) + ② 配方(每能力纵向) + ③ 知识包(技能商店)**。
   比方:**底盘 / 车型 / 驾驶手册**。
4. **当前最大的缺口不是形态,是「前半段」**:主题→调研→文案→时间轴 **完全空缺**;
   画面创作**两个极端中间是空的**(主 agent 手写 = 高质但不可无人值守;零 LLM 模板 = 幻灯片)。
5. **「用户无感知」应理解为「零编排负担」,不是「零可见性」**——底盘必须双投影进度
   (气泡内联生产卡 + 面板),否则回到「一行递增秒数」的老坑。

---

## 1. 现状体检(全部盘上核实)

### 1.1 已交付的是渲染机械层,不是主题驱动生产

`docs/MOTION_VIDEO_DESIGN.md` 声明 P0/P1/P2/P2b/P3 全交付,属实——但交付范围是
**「已有 SRT / scenes.json」之后**的那一半。

| 阶段 | 今天的状态 | 证据 |
|---|---|---|
| 意图 → 选题澄清 / 时长规格 | ❌ 无 | 无任何 intent 层(§1.3) |
| 主题 → 联网调研聚合 | ❌ 无 | engine 内零 web_search 缝 |
| 调研 → 口播文案 | ❌ 无 | `guide/WORKFLOW.md:12-17` 把这件事推给主 agent 即兴生成 |
| 文案 → 时间轴 | ⚠️ 硬估 | `lib/paper/video_abstract.py:88-99` 用 4.2 字/秒估算,**不复用 TTS 真实时长** |
| 分镜 → 画面 | ⚠️ 两极 | §1.2 |
| 配图 / 素材 | ❌ 无 | `lib/tools/image_gen.py` 与 motion_video 零连接;契约本身禁止渲染期联网取素材 |
| 渲染 / 真实转场 / 配音与整片混音 / 烧字幕 / 单镜重渲 | ✅ 完整 | `lib/motion_video/_render.py` `_timeline.py` `_concat.py` `_audio.py` `_audio_cues.py` |

**入口把门关死了**:`routes/api_v1/motion.py:100-103` 要求 `srt` / `srt_path` /
`scenes_path` 三者至少一个,否则 400;`lib/motion_video/engine.py:83-98`
`raise ValueError('neither srt_path nor scenes_path available')`。
唯一的无-SRT 通道是论文视频摘要,但前置是 `video_abstract.py:126` 的 `has_report()` 门。

### 1.2 画面质量:两个极端,中间是空的

| 路径 | composition 由谁产出 | 质量 | 代价 |
|---|---|---|---|
| P1 聊天工具路(8 个 `motion_video_*`) | **主 agent 逐镜手写 index.html** | 高:可 web_search 下载官方 SVG/logo,可用 13 个多相位蓝图 | 8 步手工编排;**必须挂项目**(`lib/tools/registry/_build.py:131-140` `project_ready` 门);烧主对话 context;**无法无人值守** |
| P3 engine 路 | 零 LLM 模板 `lib/motion_video/_template.py:34-118` | **幻灯片**:四色渐变背景 + 居中一行白字 + 左上角「01 / 08」;动画仅 headline y+56 淡入、tag 淡入(`_template.py:110-116`) | 全自动 |

模板还有两个已知边界:SRT 整段 cue 直灌 headline(`_template.py:99-101`),
长文本降到 46px(`_FONT_STEPS`,`_template.py:25`)会溢版。

**这是本设计稿要补的第一块:engine 内缺一个「每镜一个子 agent」的创作阶段。**
auto-motion 当年就是每镜起一次 Claude Code;我们有 `lib/agent_loop.run_agent_loop`
(多轮工具循环 + 三点中止检查)现成,完全可以在进程内做同一件事,且更强
(无外部 CLI 依赖 / 可并行 / 可单镜重跑)。

### 1.3 聊天侧:无意图路由,纯模型自选

- **没有任何 LLM 预分类 / intent router**。`lib/chat_dispatch.py:93`
  `classify_send_intent` 名字像路由,实际只是并发调度分类器(aborted/steered/queued)。
- 真正决定「有哪些工具」的是声明式注册表 `lib/tools/registry/_build.py:301`
  `_register_builtins()`,按 `ToolContext` 布尔开关静态拼 schema。
- `lib/tasks_pkg/chat_mode.py:74-79`:Air 已删,只剩 `chat`/`studio`,只做原子 flag 展开,
  **不做语义分流**;`is_lean_mode()` 永远 False(`:138-150`),是给未来「简单轮次自动收缩工具」留的缝。

**推论**:「说一句话就出片」不需要新造意图层——需要的是**一个语义足够明确、门槛足够低的
入口工具**,让模型在完整 schema 上自然选中它。反过来说,今天 `project_ready` 硬门意味着
**没挂项目的用户连工具都看不见**。

### 1.4 长任务在气泡里的呈现:同步阻塞 + 一行递增秒数

- `lib/tasks_pkg/handlers/motion_video.py:92-103`:`motion_video_render` **同步阻塞**
  (默认 `timeout=1800`),整段跑完才 `_finalize_tool_round` 一次性返回。
- 期间只有心跳保活:`lib/tasks_pkg/tool_dispatch/_heartbeat.py:125` 每 15s
  (`TOOL_HEARTBEAT_INTERVAL`)发一条 `tool_progress`,内容只有「渲染中…(N s)」。
  **没有阶段、没有第几镜、没有百分比。**
- **能流式的先例已存在**:`lib/tasks_pkg/handlers/code_exec.py:29-110` 把 stdout 按
  200ms/4KB 合批发 `tool_progress`,并镜像到 `round_entry['_partialOutput']` 供断线重放;
  前端 `static/js/ui/sse_handlers_io.js:49` `_handleToolProgress` 已接收。
  **motion_video 没接这条路。**
- 另有独立通道:`TaskRuntime(push_channel=…)`(`lib/agent_core/task_runtime.py:93`)
  → `push_event`,paper/translate 用它驱动独立面板,不走气泡。

### 1.5 swarm 不能当流水线执行器(硬约束)

`lib/swarm/tools.py:460` `SUB_AGENT_DENYLIST` 禁止子代理再 spawn ⇒ 只有**一层扇出**,
做不了「分镜→渲染→配音→合成」的有序多阶段编排;阶段串联若交给主模型每轮决策,
既不可靠又烧 context。**编排必须在 TaskRuntime 内,不在 swarm 内。**
(swarm 仍是**阶段内并行**的合适工具,例如「10 个镜头各自创作」。)

### 1.6 样板成本:1500–2000 行/能力,真业务只有 300–600 行

| 层 | 文件 | 真实量级 |
|---|---|---|
| runtime | `lib/<feat>/runtime.py` | **~118 行,近乎逐字复制**。`lib/motion_video/runtime.py` 的 docstring 自己写着「Mirrors `lib.paper.podcast_runtime` exactly」 |
| engine | `lib/<feat>/engine.py` | 300–600 行,**纯业务** |
| api_v1 路由 | `routes/api_v1/<feat>.py` | 300–400 行(`motion.py` 318);poll/abort 已被 `routes/_task_routes.py:36` 吃掉,剩 start 校验 + dedup + 文件下发是手写 |
| push 频道 | 一个构造参数 | ≈0 |
| 前端 API | `static/js/api.js` | ~8 行/能力 |
| 前端面板 | `static/js/paper/podcast.js` 493 行 / `paper/video.js` 467 行 | **~500 行/能力** |
| i18n | `static/js/i18n.js` | podcast 26 条 / video 30 条 × 语种 |
| bundle + tab 接线 | `lib/js_bundler.py` + `index.html` 4 处 | 逐条静态钉 |
| artifacts | **完全没接** | §1.7 |
| 测试 | | ~1200 行/能力 |

**已存在但没用起来的三个缝**:
- `routes/plugin_registry.py:57` `tofu.task_runtimes` 入口组——「注册一个后台能力」的雏形,
  只服务 `routes/api_v1/tasks.py` 的通用查询,**不生成路由不生成面板**;
- `routes/api_v1/tasks.py:40` `_registries()` 是**硬编码列表**(chat/paper/translate/agents),
  **motion 与 podcast 都不在其中**——于是 podcast 又手写了一遍 `poll_podcast_task`;
- 通用 `GET /api/v1/tasks/<id>/{events,stream}` + `/abort` 其实**已经通用化了**,没人骑。

**现成反证(证明抽象可行)**:`lib/paper/video_abstract.py` 复用 motion runtime,
全部业务只用 **152 行**,零新增路由、零新增面板。

### 1.7 artifacts today 承载不了 mp4/音频

- `lib/artifacts/core.py:37` `ALLOWED_FORMATS = ('markdown','html','svg')`,非白名单直接 `ValueError`;
- `content` 强制 `isinstance(content, str)`(`core.py:250`),**无二进制通道**,上限 8 MiB(`core.py:34`);
- 前端 `static/js/artifacts.js:428` 只分派 md/html/svg,无 `<video>`/`<audio>`;
- 于是 podcast 走 `serve_podcast_audio`(`routes/paper.py:3001`)、motion 走
  `serve_motion_file`(`routes/api_v1/motion.py:180`),**各造一套 Range 下发**。

---

## 2. 三层分工模型

```
┌─────────────────────────────────────────────────────────────┐
│ ③ 知识包(技能商店,Markdown)  「怎么做得好看」                 │
│    hyperframes-motion / -design / 分镜策略 / 品牌规范          │
│    ── 零代码。模型按需 load_skill 渐进披露。                   │
├─────────────────────────────────────────────────────────────┤
│ ② 配方(每能力 300–600 行纯业务)  「这件产品由哪些阶段组成」    │
│    video   = research → script → timeline → storyboard        │
│              → scene_author → render → assemble               │
│    podcast = research → script → tts → assemble               │
│    ppt     = research → outline → slides → export             │
├─────────────────────────────────────────────────────────────┤
│ ① 产出底盘(横向复用,一次性)  「长任务怎么跑、产物怎么存、       │
│    事实怎么保持新、内容单元如何互认、进度怎么让人看见」          │
│    job 生命周期 + 阶段图 + 证据/内容契约                         │
└─────────────────────────────────────────────────────────────┘
```

### 2.1 ① 产出底盘 `lib/production/` —— 职责清单

| 组件 | 职责 | 消灭的重复 |
|---|---|---|
| `runtime.py`(`ProductionRuntime`) | `TaskRuntime` 之上的薄层:dedup key、任务字段形状、stale 清理 | runtime 五件套 ~118 行/能力 |
| `stages.py` | **阶段图契约**:每阶段 = `(name, run, gate, retry, resume_ttl_s, checkpoint_version)`；失效上游会原子失效整个下游后缀 | 每个 engine 手写 try/except、重试、断点一致性 |
| `jobs.py` | job manifest 读写 + 崩溃后扫描重投 | 每能力各写一份 job.json 与 resume scanner |
| `research.py` | 多车道时效研究、URL 证据卡、当前状态/价格信号、跨媒介事实硬闸；月/周 freshness 是 profile | PPT/video 两套搜索与“有 URL 就算新”的漂移 |
| `contracts.py` | 叙事职责/存在理由、`S#`、素材 brief、质量 finding 的最小共用形状 | 每种媒介各自清洗同一字段且语义漂移 |
| 注册 | 挂 `tofu.task_runtimes`，通用任务 API 通过发现制拿到 runtime | 每能力手写 poll |

`progress.py`、`deliverable.py`、`entry.py` 仍未抽取；§9.3/§10.3 的第三样本证据没有
支持把它们升级为全局底盘。这里不因为内容内核统一而顺手扩大范围。

**不抽象的东西(刻意)**:
- **前端面板不统一**——podcast 的转录/睡眠定时 与 motion 的逐镜网格/单镜重渲是**真实领域差异**,
  强行统一会造出配置驱动的怪物。只统一**一张通用「生产卡」**(阶段进度 + 产物播放/下载 + 中止),
  领域面板作为它的可选展开区。
- **engine 业务不抽象**——那 300–600 行本来就该各写各的。

### 2.2 ② 配方 —— 视频配方的完整阶段(补齐前半段)

| # | 阶段 | 输入 → 输出 | 用 LLM? | 闸(零 LLM) |
|---|---|---|---|---|
| 1 | `research` | 主题 → 事实卡片集(来源 URL + 要点) | 是(web_search 多轮) | 每条要点必须挂 ≥1 个真实 URL |
| 2 | `script` | 事实卡片 → 口播稿(分段、口语化、时长预算) | 是 | 字数-时长预算 ±10%;禁空段 |
| 3 | `timeline` | 口播稿 → **真 SRT**(用 TTS 实测时长,不再 4.2 字/秒硬估) | 否 | 首尾相接、全覆盖、单调 |
| 4 | `storyboard` | SRT → scenes.json | 否(贪心)/是(可选升级) | 现成 `check_storyboard` |
| 5 | `scene_author` | 每镜 → index.html | **是(每镜一个子 agent)** | 现成 `check_composition_html` + `motion_video_check`,失败带反馈自修 1 次 |
| 6 | `render` | 每镜 HTML → mp4 | 否 | 现成 `verify_spec`(ffprobe 复核) |
| 7 | `assemble` | 逐镜 mp4 + 配音 → final.mp4 + 侧车 SRT | 否 | 现成时长复核 |

**阶段 3 的关键修正**:今天 `video_abstract.py:88-99` 用 4.2 字/秒估时间轴,
而 `lib/motion_video/_audio.py` 的 `synthesize_scene_narrations` 早就能给出**真实音频时长**。
把 TTS 前移到时间轴阶段,SRT 就不再是估算——**音画天生对齐,loose 对轴的补偿也变小**。

**阶段 5 的关键新增(本设计稿的技术核心)**:
- 每镜起一个 `run_agent_loop`(`lib/agent_loop.py`),工具集**窄到只有**:
  `write_file`(限定该镜目录)/ `web_search` / `fetch_url` / `motion_video_check` /
  `motion_video_render` / `motion_video_probe`;
- 上下文只带:该镜文案 + 时长 + `guide/COMPOSITION_CONTRACT.md` + `skeleton.html`
  + 可选激活的 `hyperframes-motion` 蓝图 —— **不带全片上下文**(与 auto-motion 每镜独立
  Claude Code 调用同构,但省掉外部 CLI);
- 失败分类已有(`_render.py` 的 env_missing/lint/chrome/timeout/aborted),
  带反馈自修最多 2 轮,再失败降级到 `_template.py`(**不整片翻车**);
- 阶段内并行用有界线程池(已有,`engine.py` 默认 2 上限 4)。
- 动画运行时是受管依赖，不是渲染期 CDN：固定版本 GSAP 先做 SHA-256 校验并
  缓存，再复制到每镜 `assets/`；作者、模板和旧断点 HTML 在闸门前统一改写为
  scene-relative 引用。浏览器代理/CDN 失效时不会再出现“首帧正常、时间轴静止”。

这一步同时解决三件事:**质量**(不再是幻灯片)、**可无人值守**(不依赖主对话)、
**主 agent 只需调一个工具**(不烧 context)。

### 2.3 ③ 知识包 —— 退为编导手册

技能商店里已有的 6 个 hyperframes 包**位置正确,只是角色要说清**:它们是
「怎么做得好看」的手册,由**阶段 5 的子 agent** 按需 `load_skill`,
而不是由主对话激活后手工照做。再加一个 tofu 自己的 `video-director` 包承载
风格偏好/品牌规范/何时该用哪种品类。

---

## 3. 外部证据(为什么执行层必须是一等公民)

| # | 证据 | 结论 |
|---|---|---|
| 1 | [agentskills.io/specification](https://agentskills.io/specification):技能 = 目录 + `SKILL.md`,可选 `scripts/`;frontmatter 必填仅 `name`/`description`,`compatibility` 是 1–500 字符**自由文本**,`metadata` 是任意 string→string。**规范完全不涉及任务生命周期、产物交付、重试与并发** | 技能包放得下脚本,承载不了「一句话→成品」的工程契约 |
| 2 | [OpenAI Background mode](https://platform.openai.com/docs/guides/background):`background=true` 创建异步任务 → 轮询 status → cancel 幂等 → `sequence_number` 断点续接,产物按 id 检索、不依赖连接存活 | 这正是底盘该对齐的语义层;Markdown 表达不了 |
| 3 | [remotion.dev/docs/ai/skills](https://www.remotion.dev/docs/ai/skills):官方 9+ 个 Agent Skills **全部是 guidance**,连 `/remotion-render` 也只讲「如何发起渲染」;真执行在 CLI / Lambda / Player,另有可 fork 的 [prompt-to-video 模板](https://www.remotion.dev/templates/prompt-to-video) | 「技能=知识层,执行=一等公民」是同类产品的既有切法 |

旁证:Anthropic 自己也承认 skill 可含代码但安全模型仅靠「可信来源 + 人工审计」
([工程博客](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills))
——不宜把长任务与外部 API 密钥路径压进只读技能包。

---

## 4. 「用户无感知」的正确读法(一处对 owner 原话的反对意见)

「不需要感知」若指**不需要编排**(不用自己切分镜、不用点八个按钮),完全对,是目标。
若指**看不到发生了什么**,会踩到 owner 在另一会话(`ms0aaxituzcl0y`)刚点名过的同一个坑
(「尤其要让用户感知到在做什么」),而且今天的呈现已经证明了这个坑
(§1.4:5 分钟只有一行递增秒数)。

**底盘的呈现契约**:

| 面 | 内容 |
|---|---|
| 气泡内联生产卡 | 当前阶段名(中文化)/ 第几镜 / 已完成镜头缩略图 / 预计剩余 / 中止按钮 |
| 断线重放 | 抄 `code_exec.py` 的 `_partialOutput` 镜像 —— 刷新后进度不丢 |
| 面板 | 可展开的领域面板(逐镜网格 / 单镜重渲 / 下载),即现成 `paper/video.js` |
| 完成 | 产物直接在气泡里可播 + 落 deliverable 库 |

**一句话**:零编排负担 ≠ 零可见性。

---

## 5. 分期实施

### P4 — 视频前半段(最高价值,不依赖底盘)
把 `research → script → timeline` 三个阶段做进 `lib/motion_video/`,并放开入口:
`POST /api/v1/motion/videos` 接受 `topic` 字段;`_build_motion_video` 的 `project_ready`
硬门改为「有项目用项目目录,无项目用 data 目录」。
**验收**:给一个新闻主题,零人工干预出片(画面仍可是模板)。

### P5 — 阶段 5 每镜子 agent(质量跃迁)
`run_agent_loop` + 窄工具集 + 现成静态闸 + 失败降级模板。
**验收**:同一主题,P5 产物与 P4 模板产物并排目检,且**任一镜失败不影响整片交付**。

### P6 — 抽出产出底盘 `lib/production/`
用 **strangler-fig**:先让 motion 骑上底盘(它最完整),podcast 保持原样;
两者并存跑绿后再迁 podcast。同期把 `_registries()` 改为发现制、
artifacts 扩 `binary` format + 文件引用 + 面板 `<video>/<audio>` 分支。
**验收**:motion 的 runtime 五件套删除、poll 走通用端点、行为逐字节等价。

### P7 — 第三个配方验证抽象
用 PPT 或长报告做第三个配方,目标:**新能力 ≤ 600 行落地**。
若做不到,说明底盘抽错了,回头修底盘而不是加配置项。

**顺序理由**:P4/P5 是 owner 真正要的东西且**不依赖**底盘;P6 的收益要等到第三个能力
才真正兑现,过早抽象会拿着两个样本猜第三个的形状。

---

## 6. 拍板记录

**已拍板(2026-07-25,owner)—— 5 项全部敲定并已在 P4/P5 实现:**

| # | 问题 | 裁定 | 落地位置 |
|---|---|---|---|
| 1 | 实施顺序 | **A:先能力后底盘** | P4 → P5 → P6 |
| 2 | `project_ready` 门 | **放开**——`produce_video` 不挂项目门 | `lib/tools/registry/_build.py` `_build_produce` |
| 3 | 阶段 5 成本上限 | **镜数 + 单镜 token 双限;不做金额上限**(金额归钱包层) | `_recipe.max_scenes` / `_scene_author.token_budget` |
| 4 | 调研事实纪律 | **强制**——每条要点 ≥1 真实 URL + 片尾来源卡 | `_recipe._gate_research` / `_sources_line` |
| 5 | 入口工具形态 | **A:`produce_video(topic=)` 先行** | `lib/tools/produce.py` |

**owner 追加的 3 条硬约束(已全部兑现):**

| # | 约束 | 兑现 |
|---|---|---|
| 1 | **崩溃续跑是正确性契约,不是成本项** | 阶段产物落盘即 checkpoint;`job.json` + 启动 `resume_interrupted_jobs`;已渲染镜头 / 已作 composition / 已合成 TTS 均不重做 |
| 2 | **事实审阅「可介入不阻塞」** | script 产物落盘可审,job 默认继续跑;拦截走现成 message-queue 抢占 + human_guidance,**不新造机械** |
| 3 | **阶段图契约现在定形,P6 是平移不是重写** | `stages.py` 写成能力无关;P6 slice 1 用 `git mv` 字节相同平移,守卫测试用**跨路径对象 identity**证明不是重实现 |

**owner 2026-07-26 裁定「第三配方先行，再抽底盘」** —— 已执行完毕。P7 不再是一个「试试看」，而是一次**测量**：拿第三个能力去撞现有底盘，撞不动的部分就是底盘已经对了，撞出来的重复就是 P6 该抽的东西。实测数据见 §9。

---

## 7. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 抽象过早 | 两个样本(podcast/motion)抽底盘,第三个不吻合 | P6 排在 P4/P5 之后;P7 用第三个配方**验证**而非事后适配;前端面板刻意不统一 |
| 成本失控 | 阶段 1 多轮调研 + 阶段 5 每镜一次 = 单条视频可能几十次 LLM 调用 | 拍板项 #3 的硬上限 + 阶段级 checkpoint(重跑不从头) |
| 事实错误 | 全自动科普视频说错话,比不出片更糟 | 拍板项 #4;片尾来源卡;`research` 阶段产物可审 |
| 渲染耗时 | 实测 ~3.1–3.6× 实时(60s 片 ≈ 3.5 分钟串行) | 已有有界并行;生产卡明确告知预计时长 |
| 共享树并发 | 本项目多 sibling 并行改同一 HEAD | 每期一个 commit、精确 pathspec、失败先行 + NEUTER(项目既有纪律) |

---

## 9. P7 实测结论 —— 第三配方对底盘的验收(2026-07-26)

owner 裁定「第三配方先行,再抽底盘」。第三个能力选**长报告**
(`lib/longform/`,主题 → 带引用的研究报告 markdown),**刻意与视频不同形**:
文本产物而非二进制渲染、无 TTS、无逐镜扇出,而且**阶段列表是数据驱动的**
(大纲有几节就有几个 section 阶段)——静态的视频阶段列表从没压过这种形状。

### 9.1 规模:512 行,达标

| 文件 | 行数 | 性质 |
|---|---|---|
| `recipe.py` | 228 | **纯业务**(research → outline → sections×N → assemble) |
| `engine.py` | 155 | worker + artifact 发布 + 崩溃续跑 |
| `runtime.py` | 99 | TaskRuntime 五件套 |
| `__init__.py` | 30 | 门面 |
| **合计** | **512** | 目标 ≤600 ✅ |

### 9.2 底盘**已经对了**的部分(直接骑,零改动)

| 能力 | 证据 |
|---|---|
| **阶段图 + 崩溃续跑** | 数据驱动的阶段列表**无需改底盘**就跑通:配方分两趟跑图、共用一个 checkpoint,第二趟从盘上跳过 research/outline。测试 `test_data_dependent_stage_list_rides_the_existing_resume_contract` 钉死;NEUTER 破坏 `stage_is_done` → 该测试与「崩溃后不重写已完成小节」双双翻红 |
| **零 LLM 闸** | 事实纪律(每条要点挂真实 URL)、小节过短拒收,全部复用 `Stage.gate` 契约 |
| **通用任务端点** | **本能力零条自建 poll/abort 路由** —— slice 2 的发现制让 `/api/v1/tasks/*` 直接可用。测试 AST 断言 `engine.py` 里没有 `Blueprint`/`route(`。对照:podcast 写在发现制之前,被迫手写 `poll_podcast_task` |
| **底盘纯净度** | AST 断言 `lib/production/stages.py` 不导入 longform/motion_video/paper/tts —— 「横向层」是真的 |

### 9.3 底盘**还缺**的部分(这就是 P6 该抽的清单,有实测支撑)

| 重复项 | motion | longform | 判定 |
|---|---|---|---|
| `runtime.py` 五件套 | 95 行 | 78 行 | **改名后 67% 逐字相同** → `ProductionRuntime` 确认该抽 |
| job 清单落盘 | 20 行 | 16 行 | 同形 → 该进底盘 |
| 崩溃重投扫描器 | 55 行 | 28 行 | 同形(扫 `jobs/*/job.json` 找 `state=running`) → 该进底盘 |
| **小计** | | **~170 / 512 行** | 三分之一的新能力代码是底盘形状的样板 |

**结论:P6 剩余部分的抽取目标,现在有三个样本而不是两个,且形状已被实测收敛为
「ProductionRuntime(含 dedup)+ 清单/重投」这一簇。** 相反,`deliverable` 二进制通道
**第三个样本没有用到**(长报告走 markdown artifact 就够了),说明它是视频/播客的
共性而非全局共性——抽它的优先级应低于 runtime 簇,这正是「两个样本会抽错形状」
风险的一次实证命中。

---

## 10. P6 抽取落地(2026-07-26,commit `424d9c28`)

owner 裁定「Go P6」。**抽什么由 §9 的实测决定,不由感觉决定** —— 这正是先跑第三配方的意义。

### 10.1 抽了什么(两簇,各有实测依据)

| 模块 | 内容 | 依据 |
|---|---|---|
| `lib/production/runtime.py` | `ProductionRuntime`:dedup 索引(带活性检查+自清理)、create+字段形状、append+touch、按 `updated_at` 的 stale 清扫、id 铸造。是 `TaskRuntime` **之上的薄层**,不是替代 | §9:三个样本的 `runtime.py` 改名后 **67% 逐字相同** |
| `lib/production/jobs.py` | job 清单读写 + 崩溃重投扫描 | §9:motion(20/55 行)与 longform(16/28 行)**同形**。崩溃续跑是正确性契约,扫描器不该每个能力各写一份 |

### 10.2 三个能力全部迁移,门面名一字未改

motion-video / paper-podcast / longform-report 三家的 `_X_runtime` / `_X_tasks` /
`_X_tasks_lock` / `_X_dedup_index` / `_new_X_task` … **全部照旧可用**,且 `_X_runtime`
仍是发现制找到的**同一个 `TaskRuntime` 对象**。

**最强证据:七个相关套件 126 测全绿,迁移本身没要求改任何一行测试。**

### 10.3 刻意没抽的(以及为什么这条比抽了什么更重要)

`deliverable` 二进制通道**没抽**。第三个样本(markdown 报告)压根没用上它 ——
它是视频/播客的共性,不是全局共性。抽它就会正中 §7 风险表预言的
「样本太少导致形状抽错」。包 docstring **明写**这一点,并有测试钉住它确实没被抽,
**免得后来人把「刻意划界」误读成「疏漏」**。同理暂缓:进度双投影、artifacts binary。

### 10.4 账要老实算

| | 抽取前 | 抽取后 | 差 |
|---|---|---|---|
| 三家能力代码合计 | 1161 行 | 1033 行 | **−128** |
| 底盘(三家共享) | — | 297 行 | +297 |

**净行数是增加的。** 收益不在当下的行数,而在**边际成本**:实测第 4 个能力的 runtime 层
现在是 **8 行**(用一次性探针验过),而不是 ~100 行。三个样本摊薄不了 297 行的固定成本,
第四、第五个才会。这是诚实的账,不是「减少了 128 行」的漂亮话。

---

## 8. 与既有设计稿的关系

- `docs/MOTION_VIDEO_DESIGN.md`:**保留不动**,它是渲染机械层的权威记录(P0–P3)。
  本稿是它的**上位抽象 + 前半段补全**,不推翻其任何结论。
- `docs/PAPER_PODCAST_DESIGN.md`:播客链是第二个「配方」样本;P6 迁移时对齐。
- `docs/ARCHITECTURE.md` / `CLAUDE.md`:P6 slice 1 落地时已在 `CLAUDE.md` 目录树新增
  `motion_video/` 与 `production/` 条目(含「哪些还没搬」的诚实边界)。

---

## 11. P8 PPT / 视频共用内容内核（2026-08-10）

澎程 deck 的时效与素材关联复盘暴露了一个跨媒介架构问题：PPT 已经有多车道研究、
来源 ID 和当前事实闸，视频仍只有“本周搜索 + 至少一个 URL”。同一个主题换成视频，
就可能重新丢掉已经修好的价格状态与证据关联。P8 把这类能力下沉到
`lib/production/`，不是把 PPT 代码复制给视频。

```
用户主题
  → production.research：清理创作指令 → current / official / background
  → evidence bundle：S# + URL + 日期 + lane + as_of + current signals
  → production.contracts：narrative role/why + source_ids + asset briefs
  ├─ slides adapter：outline → PPTD 静态布局 → PPTX
  └─ motion adapter：spoken beats → 真音频时间轴 → HyperFrames / MP4
  → shared finding shape：check / element / issue / severity / fix
```

### 11.1 真正共用的部分

- **研究与证据：** 同一个 `research_topic()`；PPT 使用近一月 profile，新闻视频使用
  近一周 profile，并在常青主题不足三卡时回退不限时。两边都保留官方候选深挖、
  背景材料、检索错误与研究截止时间。
- **事实闸与定向重试：** `current_fact_errors()` 同时审 deck outline 和视频脚本。
  有当前状态必须引用对应 `S#`；已有预售价却说未公布、已有发布证据却说等待发布，
  都会拒绝，并把原因回灌下一轮模型。
- **内容单元：** 页和镜头都携带 `narrative_role` + `narrative_why`；视频逐镜保留
  `source_ids` 直到 scenes.json，片尾来源卡不再替代逐镜证据关联。
- **素材与 QA：** 两种媒介的 `role/prompt/semantic_target` 素材 brief 与结构化 VLM
  finding 经过同一 normalizer；`semantic_target` 必须说明素材具体支撑哪个可见对象、
  部位或关系，unknown role 不得悄悄创造新的强制素材等级。
- **断点策略：** 研究 checkpoint 共用 6 小时 TTL 和 evidence schema 版本，版本再按
  week/month profile 分叉；任何失效都会带着下游后缀一起重跑。

### 11.2 明确不共用的部分

PPTD 的边界、字形行框、OOXML 导出属于**静态空间语义**；HyperFrames、GSAP、TTS、
镜头 duration 与拼接属于**时间语义**。两者是 production content kernel 的两个媒介
adapter，不共享授权语言或 renderer。统一的判断标准是：同一缺陷若换一种媒介仍成立，
才进入底盘；只在页面或时间轴上成立，就留在能力内。

---

## 12. P9 renderer-neutral 视频镜头合同（2026-08-10）

`video-shotcraft` 和 Remotion 的共同启发不是替换技术栈，而是在脚本语义与具体 renderer
之间建立结构化 shot IR。P9 将 managed HyperFrames corpus 的 13 个蓝图升级为
`motion-shot-v1` 配方目录；每镜在进入作者与渲染器前都具备运动族、能量、建议时长、
多相位数量、QA 锚点、落定停留和验收约束。自动规划会在语义允许时改变相邻运动族，
显式选择则保留并产生可见 finding，避免“为了去重擅改创意”。

这层只属于 motion adapter，不下沉到 `production/contracts.py`：页面没有帧、hold、phase
或 transition-overlap 语义。共用底盘仍只拥有跨媒介成立的证据、叙事、素材和 finding
合同。目录经 `GET /api/v1/motion/shot-recipes` 暴露，因此 HyperFrames、未来 canvas 或
其他帧引擎可以消费相同 recipe，而无需把 renderer 依赖塞进内容层。

QA 同步从固定 15/50/90 三帧变成每配方四个语义锚点，并增加字体与图片的 render-ready
闸。

---

## 13. P10 program timeline 与 film-level audio IR（2026-08-10）

P10 吸收 Remotion `TransitionSeries` 的时长代数和 video-shotcraft 的集中 sound table，
但保留 HyperFrames renderer。`motion-timeline-v1` 将每镜拆成 content duration 与可选的
outgoing visual handle：进入下一镜的 overlap 等量加到前镜渲染尾部，随后由真实 FFmpeg
`xfade` 消耗。这样 program duration、TTS、SRT 和 SFX cue 仍使用同一连续时钟，转场不会
偷偷缩短成片。

`motion-audio-v1` 是 motion adapter 独有的整片音频合同：本地 BGM/SFX 经过授权校验、
SHA-256 staging 与漂移复核；cue 可以按绝对秒、镜头进度、镜头 offset 或 verified beat
定位，并把音效内部 peak 对齐到可见 action。混音链包含 narration/BGM gain、旁白触发的
sidechain ducking、淡入淡出、SFX delay、limiter 与目标 LUFS，最终同时交付机器可复现的
plan 和人可读归因文件。verified beat grid 必须携带逐拍观测，由系统重算残差、匹配率、
平均误差、漂移与首拍五项门槛；required 模式将超过三帧的转场节拍误差视为合同失败。

这两层不进入跨媒介 production kernel：PPT 没有帧、beat 或 overlap handle。PPT 与视频
继续共用事实、叙事、素材语义和 VLM finding，之后才各自由静态空间 adapter 与时间 adapter
解释。当前系统不自动检测音乐节拍，也不捆绑第三方音效库；它验证并消费明确提供的节拍与
有来源素材。
