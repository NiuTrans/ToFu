# 论文播客与视频:生成链路与前端呈现(进度感知 / 防卡死)设计稿

> 状态:**设计稿,待 owner 拍板**(2026-07-25 落笔)。
> 触发诉求(owner 原话):「carefully design the way podcasts and videos are
> generated, as well as how they are presented to the frontend (especially
> to make users perceive what is happening and not get stuck constantly)」。
> 上位稿:`docs/PRODUCTION_PIPELINE_DESIGN.md`(产出底盘,P4–P7 分期,
> epic `pt_17a41dba5dec476e`)——它解决「一句话 → 成品」的前半段与抽象;
> **本稿只管两件事:① 播客/视频这两条已有链路的生成设计收口,
> ② 生成过程怎么呈现给用户(感知 + 永不卡死)。** 不重复上位稿内容。

---

## 0. 结论先行

1. **生成链路本体是健全的**(播客:剧本 LLM + 6 道零-LLM 质检闸 + TTS;
   视频:分镜 → 配音 → 构图 → 渲染 → 合成 → 混音,每阶段有闸)。
   真正缺的是**进度语义**:长阶段零事件,前端只有一行静态文字。
2. **「卡死」不是一个 bug,是三形态系统性缺口**——① 轮询 404/断网时前端
   **无限重试、转圈到永远**;② 长阶段(LLM 写剧本 1–3 分钟、逐镜渲染
   ~35s/镜)期间**零事件**,看起来就是死了;③ 服务器重启/worker 崩死
   → 任务永远停在 running,lookup 失忆。三形态都有实锤代码证据(§1.2)。
3. **修法不是加 spinner,是给进度一个协议**:统一「阶段 + 计数 + 心跳」
   事件词汇,前端从「显示一句话」升级为「渲染一条流水线」——阶段步进器、
   逐镜网格边生成边填、已用时间、超时老实提示、失败有终态。
4. **防卡死的最后一道必须在前端**:任何轮询都有失败时,连续 K 次失败
   进入 `lost` 诚实态(「任务丢失或连接断开」+ 重查/重新生成按钮)——
   **永远不再无限转圈**。

比方:今天像「外卖 App 只显示『骑手配送中』五个字,四十分钟没变化,
你不知道骑手是到了楼下还是掉线了」;目标是「地图上有骑手在动,
你知道他到哪了、还要多久、真掉线了会明确告诉你并给退款按钮」。

---

## 1. 现状体检(全部盘上核实)

### 1.1 生成链路:两条都已成型,进度语义贫瘠

**播客链**(`lib/paper/podcast_engine/__init__.py:_run_podcast_task`):

```
素材(报告→译文→原文) → script(LLM 多轮 + 6 道质检闸) → TTS 逐段合成
→ 原子落盘 → DB 缓存行 → done / script_only(无 TTS 槽位时诚实降级)
```

事件:`phase(script)` → `script` → `phase(audio, total)` →
`segment_done(d/t)` × N → `audio_ready` → `done`。
**缺口**:script 阶段是 1–3 分钟的 LLM 多轮调用(含 critic 修订),
期间**只发一条 `phase(script)` 事件就没有然后了**。

**视频链**(`lib/motion_video/engine.py:run_motion_task`):

```
parse → storyboard → narrate?(逐镜 TTS) → compose(模板) → render
(有界并行,逐镜 scene_done) → concat → sidecar → burn_in? → mux?
```

事件:`phase(每阶段一条)` + render 阶段的 `scene_done(d/t)`。
**缺口**:narrate(逐镜 TTS,`_audio.py` 整段跑完才发 manifest 事件)、
compose、concat、mux 都是**零事件黑箱**;render 阶段两镜之间隔 ~35s
(`3.5×` 实时)也无任何信号。

**已有零件(好用的先例)**:播客的 TTS 逐段 `segment_done` 计数、无 TTS
槽位时的诚实降级横幅(script_only + banner)、视频失败分类
(env_missing/lint/chrome/timeout)——证明「阶段 + 计数 + 诚实态」模式
在代码库里已经存在,只是没有贯穿全程。

### 1.2 「卡死」三形态(每条都有代码实锤)

| # | 形态 | 证据 | 后果 |
|---|---|---|---|
| ① | **404 → 无限转圈** | 前端轮询全部 `onError:'null'`(`api.js:1180-1182` podcast、`api.js:1204-1206` motion);404 时 resolve 为 `null` → `podcast.js:113` / `video.js:114` `if (!resp \|\| !resp.ok) { schedulePoll(); return; }` **无条件 reschedule,无计数无上限** | 服务器重启 / 任务被 TTL 清理 / 断网后,用户面对一个**永远转圈的 spinner**,只能刷新页面自救 |
| ② | **长阶段零信号** | 播客 script 阶段:LLM 多轮(`_script.py` 生成→校验→critic 修订)1–3 分钟,`_run_podcast_task` 只发 `phase(script)` 一条;视频 narrate/compose/concat/mux 同理;render 两镜间 ~35s 静默 | 进度行停在静态文字(「Writing the spoken script…」),用户无法区分「在跑」与「死了」 |
| ③ | **running 永固 + 失忆** | 任务全部在内存(`podcast_runtime.py` / `motion/runtime.py`,TTL 1h);服务端没有任何「running 但久未更新 → 判死」机制;视频 lookup(`routes/paper.py:2871-2902`)**只扫内存表**,重启后磁盘上的成品 mp4/scenes.json 都在却 `found: False` | worker 崩死 → 状态永远 running,前端陪跑到天荒地老;重启 → 已完成作品从前端消失 |

### 1.3 小问题(顺带收口)

- **视频 start 无 dedup**:`video_abstract.py:start_video_abstract` 每次新建
  任务,不走 `_motion_index_get/_register`(motion 主路由 `motion.py:152-157`
  有 dedup,paper 入口漏了)——两个浏览器/双击会起两条并行渲染。
- **重复播客页签事故(已修)**:index.html 里 podcast 页签按钮/面板/script
  标签被锚点复制成两份(用户截图即此),已在 HEAD 去重 +
  `tests/test_paper_tabs_unique.py` 守卫入库(74718bce);
  `engine.py` 同款死重复 except 已清(a88b24ed)。

---

## 2. 生成链路设计(收口,不推翻)

播客链和视频链的阶段划分**保持现状**——它们已经是对的。收口只有三处:

### 2.1 视频 start 补 dedup(顺手,10 行)

`start_video_abstract` 加 dedup key `(paper_hash, lang, voice, narration,
burn_in, quality)`:有 live 任务则 join(返回同一 task_id),与 motion 主路由
行为对齐。「Regenerate」按钮显式绕过(dedup 之外新建,与播客 `force` 一致)。

### 2.2 视频时间轴用 TTS 真实时长(已列入上位稿 P4,此处重申为本链缺口)

`video_abstract.py:35` 的 4.2 字/秒硬估在 narrate 阶段即被真实音频时长推翻
(loose 模式),说明**估算是多余的中间误差**。把 TTS 前移到分镜后即做,
SRT/场景时长直接用实测值,音画天生对齐。(实施归上位稿 P4,本稿不展开。)

### 2.3 播客 script 阶段拆出可报告子阶段

`_script.py` 内部本就是「初稿 → 质检 → 修订 → critic」的循环,把这些
子步骤作为 `progress` 事件的 `unit` 上报(见 §3.1),不需要改生成逻辑本身。

---

## 3. 进度呈现契约(本稿核心)

### 3.1 统一进度事件词汇(两条链共用)

在现有事件之上**增量**三类,不破坏存量消费者(前端按 `type` 分发,未知类型忽略):

| 事件 | 载荷 | 语义 | 成本 |
|---|---|---|---|
| `phase_started` | `{phase, phase_index, phase_total, label_key, started_at}` | 进入阶段;`label_key` 是 i18n 键,**文案归前端** | 零 |
| `progress` | `{phase, done, total, unit}` | 阶段内计数;`unit` ∈ `scene/segment/pass/revision` | 零 |
| `heartbeat` | `{phase, elapsed_s}` | 长阶段每 10s 一条,**证明 worker 活着** | 零 LLM |

现有 `phase` / `scene_done` / `segment_done` / `script` / `done` / `error`
原样保留(向后兼容,老前端不受影响)。

**心跳的实现位置**:不是给每个阶段手写定时器,而是在 worker 侧加一个
10 行的 `_Heartbeat` contextmanager(`threading.Event.wait(10)` 循环 +
`_append_*_event`),长阶段 `with heartbeat(task, 'script'):` 包住即可。
事件表膨胀:5 分钟任务 ≈ 30 条心跳,cursor 协议天然消化。

### 3.2 服务端:read-side stall 收割(防「running 永固」)

不给 runtime 加后台线程,而是**在 poll 响应路径上收割**(有前端看着才需要判死,
self-healing 且零常驻成本):

```
poll(task) 时:if status == 'running' and now - updated_at > STALL_TIMEOUT:
    finish(error='worker_lost', error_context='<feature>:stall')
```

`STALL_TIMEOUT` 取 **3× 心跳间隔 + 长阶段下限 = 120s**(心跳 10s 一条,
120s 无任何事件=真死;渲染单镜 ~35s、script 阶段心跳兜底,不会误杀)。
落点:podcast 手写 poll(`routes/paper.py:2904-2910`)+
`register_task_routes` 工厂(`routes/_task_routes.py:71`)各加一处,
或更优雅地在 `TaskRuntime.poll()` 内统一——**一次实现,所有 runtime 受益**。

### 3.3 重启韧性(防「失忆」)

| 链 | 机制 | 改动量 |
|---|---|---|
| 播客 | DB 缓存行已有(done/script_only);**增**:任务开始时先落一行 `status='generating'`,完成时覆写;服务端启动时把 `generating` 行批量改 `interrupted`;lookup 命中 `interrupted` → 前端诚实显示「上次生成被服务器重启打断」+ 重新生成按钮 | `_persist_podcast_row` 复用,~30 行 |
| 视频 | workdir 落 `job.json` 清单(paper_hash/参数/status,创建时写、done 时更新);lookup 扫内存落空后**回落扫 `motion_root/jobs/*/job.json`** 取最新;status=running 的磁盘任务一律视为 interrupted(重启即死) | `video_abstract.py` + lookup,~50 行 |

### 3.4 前端:从「一行字」到「一条流水线」

**A. 阶段步进器**(两链各自一张,DOM 结构共用):

```
播客:  素材 ✓ → 剧本 ✓ → 质检修订 ●(第 2 轮) → 配音 3/12 → 完成
视频:  解析 ✓ → 分镜 ✓ → 配音 ✓ → 构图 ✓ → 渲染 5/8 ● → 合成 → 混音
```

已完成打勾、当前高亮、未到灰显——用户一眼知道「全程有几步、现在在哪」。
数据源:`phase_started` 的 `phase_index/phase_total` + `progress` 计数。

**B. 活性指示(回答「还活着吗」)**:
- 进度行常驻**已用时间**(前端本地秒表,事件到达即校零);
- 收到 `heartbeat` 更新「最后活动 Xs 前」;
- **超过 30s 无任何事件**:进度行变黄并显示「仍在运行,最后活动 Xs 前
  (这步通常要 N 分钟)」——把「安静」和「死亡」在视觉上分开;
- 收到 `error=worker_lost` → 红态「任务中断(工作进程丢失),可重新生成」。

**C. ETA(诚实标注「约」)**:render 阶段用已完成镜的平均耗时 × 剩余 / 并行度;
TTS 阶段同理(已合成段均值 × 剩余段)。其它阶段不显示 ETA(不编数字)。

**D. 视频:逐镜网格边生成边填**。今天网格只在 done 后加载
(`video.js:_pvLoadScenes`)。改为 generating 期间按 `scene_done` 事件增量刷
`/scenes` 端点:网格骨架先按 storyboard 数摆出占位格,每镜渲染完成即点亮
缩略图——**用户看着镜头一个个跳出来**,这是最强的「在做事」信号。
完成时网格无缝保留,播放器出现在上方。

**E. 终态兜底(防卡死的最后一道,必须做)**:

```
连续 5 次轮询失败(404 或网络错)→ 停轮询,进入 lost 态:
  「任务丢失或连接已断开」
  [重新查询状态](重跑一次 lookup)  [重新生成]
```

任何路径下 spinner 都有寿命上限。**没有终态的重试就是卡死。**

**F. 中止语义**:abort 后立即落 idle;已产出的部分不丢——播客若 script 已生成
则展示剧本(script_only 视图复用);视频已渲镜头保留在网格,可单镜继续。

### 3.5 呈现契约汇总(「零编排负担 ≠ 零可见性」的落地)

| 面 | 内容 |
|---|---|
| 步骤感 | 阶段步进器(全程几步/当前第几步) |
| 活性 | 已用时间 + 最后活动 Xs 前 + 心跳 |
| 量化 | 阶段内 done/total + 诚实 ETA |
| 可见产出 | 逐镜网格边渲边填;剧本生成即可读 |
| 诚实降级 | 无 TTS→script_only 横幅(已有);worker 死→红态;重启→interrupted 态 |
| 永有终态 | 轮询 5 连败 → lost 态;任何状态都有 [中止]/[重查]/[重新生成] |

---

## 4. 分期实施

### P-UX1 — 终态兜底 + stall 收割(最高性价比,纯防卡死)
前端两 tab 加轮询连败计数 → lost 态;`TaskRuntime.poll()` 加 read-side
stall 收割(120s)。**验收**:① 任务中途 `kill -9` worker/重启服务器,
前端 2 分钟内落到诚实红态而非转圈;② 手动断网 30s 恢复,不出现永久 spinner。

### P-UX2 — 心跳 + 阶段步进器
worker 侧 `_Heartbeat` contextmanager 包住 podcast script / video narrate /
compose / concat / mux;`phase_started` 事件;两 tab 渲染步进器 + 已用时间 +
「最后活动 Xs 前」。**验收**:script 阶段 90s 无 LLM 返回时,UI 每 10s
仍有活性更新;全程能看到当前阶段名与序号。

### P-UX3 — 视频逐镜网格边渲边填 + narrate/compose 计数事件
`scene_done` 驱动增量刷网格;`_audio.py` narrate 逐镜发 `progress`。
**验收**:8 镜任务,用户能看到缩略图逐个点亮,网格填充与 scene_done 一一对应。

### P-UX4 — 重启韧性(磁盘/DB 清单 + interrupted 态)
播客 generating 行 + 启动批量 interrupted;视频 job.json + lookup 磁盘回落;
前端 interrupted 诚实态。**验收**:生成中重启服务器,重开页面显示「被重启
打断」并可一键重新生成;done 态产物重启后仍可播放。

**顺序理由**:P-UX1 消灭最痛的「永久转圈」且不依赖任何事件改造;
P-UX2/P-UX3 是感知主力;P-UX4 是长尾兜底。四期各自独立可交付、可回滚。

---

## 5. 待拍板

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| 1 | **轮询连败上限** | (A) 5 次连败 → lost 态 / (B) 指数退避但永不放弃 / (C) 可调 | **A**:spinner 必须有寿命;B 就是今天的卡死 |
| 2 | **心跳落事件表 vs poll 响应合成** | (A) 落表(断线重放可见,~30 条/任务) / (B) 不落表,poll 时按 updated_at 合成(零膨胀但重放无历史) | **A**:重放语义完整,膨胀可忽略 |
| 3 | **ETA 展示范围** | (A) 仅 render/TTS 两阶段(有实测均值) / (B) 全阶段(静态经验值) / (C) 不显示 | **A**:不编数字;B 的静态值在这台机器上实测方差太大 |

---

## 6. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 事件协议膨胀 | 三类新事件进事件表 | 未知 type 前端忽略,向后兼容;心跳只在长阶段包 |
| stall 误杀 | 120s 无事件判死,慢 LLM 首 token 可能接近 | script 阶段由心跳兜底,事件不停就不会误杀;阈值取 12× 心跳 |
| 前端状态机复杂度 | 两 tab 各 ~500 行,加步进器/lost 态 | 状态机已存在(播客 6 态/视频 7 态),lost 只是第 7/8 态;步进器是纯渲染函数 |
| 与上位稿 P6 重叠 | 产出底盘的 `progress.py` 双投影与本稿 §3.1 词汇 | 本稿事件词汇即底盘词汇的种子实现;P6 抽底盘时收敛,不冲突 |

---

## 7. 与既有设计稿的关系

- `docs/PRODUCTION_PIPELINE_DESIGN.md`:**上位稿,不动**。本稿 §2.2 引用其
  P4 结论;§3 的事件词汇是其 `progress.py` 的具体化;P-UX 分期与其 P4–P7
  正交(本稿全部落在现有两条链上,不等底盘)。
- `docs/MOTION_VIDEO_DESIGN.md` / `docs/PAPER_PODCAST_DESIGN.md`:渲染/生成
  机械层权威记录,本稿不推翻,只补「进度语义 + 呈现」层。
