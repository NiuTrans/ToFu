# SLIDES_CAPABILITY_DESIGN.md — 对话内精美 PPT 生成 + motion_video 设计系统升级

> 状态：设计评审稿（2026-08-06）。起源：owner 指令「不要依赖 Kimi，借
> open-kimi-ppt-skill 增强 video 生成能力，并在对话界面支持直接生成非常精美
> 有设计感的 PPT」。
>
> 参考仓库：`../open-kimi-ppt-skill`（MIT，`Binaryify/open-kimi-ppt-skill`
> v1.0.1，逆向 Kimi Slides 的非官方 Skill）。其渲染/导出层本质是 iframe 驱动
> Kimi 线上编辑器（`export_host.html` → `www.kimi.com/neo-ppt/` + penpal RPC，
> PPTD→OOXML 写出器在 Kimi 前端 bundle 里），**不可搬、不可 vendor**（闭源、
> 资源带哈希版本号、协议无兼容承诺）。本设计的全部渲染与导出均为自研，零 Kimi
> 依赖。

---

## 1. 根因诊断：为什么 Kimi 的 PPT「精美」，我们的 video「效果差」

对 DJI 示例 deck（18 页真实产出）逐页拆解 + 对本仓库 motion_video 全链路
重读后，差距集中在四层，按视觉影响排序：

| # | 差距层 | Kimi/参考仓的做法 | 我们的现状（证据） |
|---|---|---|---|
| 1 | **字体系统** | `reference/fonts.md`：10+ 款精选中文字体（MiSans/阿里妈妈数黑体/得意黑/站酷文艺体/LXGW…）+ 9 款拉丁字体，**按场景配对** | `lib/motion_video/_fonts.py`：全库只有一款 Noto Sans SC 子集。一款字体撑所有题材 = 天然「通用感」，衬线回退事故刚修过 |
| 2 | **场景设计圣经** | `reference/slides_categories/*.md`：7 大场景 × 100-250 行强观点指南（叙事骨架、页面节奏、图表纪律、**反 AI 味禁令**：禁卡片堆层级、禁蓝紫渐变、禁 2×2 矩阵套路） | `guide/MOTION_CRAFT.md` 只有一份通用指南，无场景分化；模板背景甚至是 4 款渐变轮播（`_template.py` `_GRADIENTS`） |
| 3 | **主题一致性** | PPTD `Theme`：一份 `$primary/$bg/$text/$accent` token 贯穿全 deck | 每个 scene 作者各自发明配色，整片无统一主题约束 |
| 4 | **视觉质检闭环** | SKILL.md step4：渲染整页图 → 多模态模型按 7 项清单（变形/遮挡/出界/对比度/排版/溢出/压图）逐页核查 → 修复 → 复检，全过才导出 | 只有程序化闸门（lint/validate/inspect/fill 全零 LLM）。「好看」没有任何一环负责 |

我们的渲染底盘（HyperFrames/headless Chrome）、资产库（`_assets.py` 内容寻址 +
三层 materialise）、质量基线（`_quality.py` 资产地板 + 遥测 + 防退化）、
stage 契约（`lib/production/stages.py` checkpoint）都是好的——**缺的是
「设计系统」与「审美验收」两个软件层，不是渲染管线**。

---

## 2. 从参考仓拿什么、不拿什么

**拿（MIT 许可，适配后入树并注明出处）：**

1. **PPTD DSL 规范**（1886 行）——OOXML 之上的 YAML 抽象：960×540 画布、
   元素绝对定位 `bounds:[x,y,w,h]`、主题 token、元素类型
   text/shape/line/image/icon/table/chart。为 AI 写作而生，比让模型直写
   OOXML/pptxgenjs 稳一个数量级。**采用其子集作为我们的幻灯片 DSL**（§4.2）。
2. **场景设计圣经**（7 份场景文档 + general-poster）——移植为
   「设计圣经」资产，同时喂给 slides 作者和 motion_video 场景作者。
3. **字体系统表**（fonts.md）——作为我们字体库的选品清单（许可证逐款核验，
   见 §3.1）。
4. **视觉质检工作流**（SKILL.md step4 的清单与循环）——移植为共享 QA 阶段。
5. **测试金样**：`example/dji-pocket4` 完整 18 页 PPTD 工程 + 成品 PPTX，
   是我们渲染器/导出器的现成对照语料（MIT）。
6. **淡入淡出切换的 OOXML 后处理手法**（`export_pptx.py` 的
   `patch_transitions`：CT_Slide 子序 cSld→clrMapOvr→transition→timing/extLst
   校验）——思路借鉴，代码自写。

**不拿：**

- `editor/`（iframe 壳，编辑器本身是 Kimi 线上资产）；
- `scripts/export_*.py` 的浏览器 RPC 主路（点 Kimi 编辑器「导出」按钮）；
- SKILL.md 中一切 Kimi 依赖步骤。
- **不做自家网页版 PPT 编辑器**（v1 明确排除）：交付真实可编辑 PPTX 的意义
  就是 PowerPoint/WPS 是编辑器；对话内迭代编辑（「把第 3 页标题改成…」→
  单页重生成重导出）是对话原生且便宜得多的编辑回路。

---

## 3. 共享层：设计系统（Epic A，video 与 slides 共用）

### 3.1 字体库 `lib/design_sys/fonts.py`（升级 `_fonts.py`）

- 字体注册表：每款字体 = `{family, 文件源, license, 场景标签, 字重}`；
  字节一次性入 `_assets.py` 内容寻址库，场景内 `@font-face` 引用
  （已验证的既有通道，不动渲染器）。
- 选品来源 = fonts.md 清单，**逐款核验可再分发许可**（思源/LXGW=OFL；
  MiSans/阿里妈妈系/站酷/得意黑官方声明免费商用——入库时必须把 license
  文件一并存证，核验不过的字体不进注册表）。
- 场景→字体配对表（如 科技=MiSans 类几何黑体，文化=思源宋体，国潮=书法体），
  由「设计圣经」选择阶段输出。

### 3.2 设计圣经 `lib/design_sys/bibles/`

- 7 份场景指南移植 + 本地化（中文为主，去 Kimi 特定引用），外加从
  MOTION_CRAFT.md 沉淀的通用排版/动效纪律。
- **主题 token 块**：每份圣经带一组候选主题（底色/主色/强调色/正文字体/
  标题字体），recipe 在「定设计方向」阶段选定一份，随后**每一个页面/场景
  作者拿到的是同一份主题块**——从根上消灭逐场景配色轮盘。
- 反 AI 味禁令（禁卡片堆、禁蓝紫渐变、禁玻璃拟态辉光、禁 2×2 套路）进
  作者 prompt 的硬约束段，并进质检清单。

### 3.3 视觉质检 `lib/design_sys/visual_qa.py`（共享新阶段）

- 输入：一页/一帧的渲染截图（headless Chrome，复用 `_env.py` 的浏览器发现）。
- 多模态模型按清单核查：变形/遮挡/出界/对比度/排版统一/文字溢出/压关键画面
  + 反 AI 味项 + 主题一致性（与既定主题块比对）。
- 输出结构化 findings（页码/元素/问题/修法建议），喂回作者修复循环；
  每页最多 N 轮（默认 2），**永不阻塞交付**——超时/模型不可用时降级为
  仅程序化闸门，并在产物元数据里标记 `visual_qa: skipped`。
- 模型选择走既有 provider 链（需图像输入能力）；零图像能力时自动跳过。

在 VLM 之前另有确定性的 `lib/slides/layout_qa.py`：同一 HTML 预览在
webfont 加载完成后读取每个文本节点的真实 `Range.getClientRects()`，逐页检查
字形越出文本框与不同文本元素的实际行框碰撞。它不拿整个 bounds 相交冒充
遮挡，因而能避开相邻但没有碰字的误报。由于 PowerPoint 与 Chromium 对同一
CJK 字体可能选择不同的 ascent/descent 表，多行文本另保留最高半个 em 的
Office 安全区；同时按 DOM/OOXML 层级检查**后置图片**是否进入这块安全区，
捕获“Chrome 里差几像素没碰、PPT 里溢出后被图片盖住”的问题。确属品牌排版的装饰字符可在单元素上
显式写 `allowOverlap: true`，但该标记不豁免溢出。findings 会带元素 ID 回到
当前页面 YAML 的最小修复回路，候选只有在发现数下降时才落盘。

### 3.4 生产内容内核 `lib/production/{research,contracts}.py`

PPT 与视频共用的层不止字体和 VLM。两边现在消费同一种 evidence bundle：每张卡有
稳定 `S#`、真实 URL、发布日期、检索车道与研究截止时间，并用同一个当前事实闸检查
发布/预售/价格矛盾。月度（deck）和周度（news video）只是 freshness profile，不能
各维护一套搜索实现。页/镜头也共用 `narrative_role + narrative_why`、素材
`role/prompt`、`source_ids` 和结构化 quality finding 契约。

边界保持严格：PPTD/OOXML/字形行框是静态空间 adapter；HyperFrames/TTS/时间轴是
视频 adapter。共用内容内核不拥有媒介 renderer，避免“为了复用”把 PPTD 强塞进动效。

---

## 4. Epic B：对话内 PPT 能力 `lib/slides/`

### 4.1 形态

- 新工具 `produce_slides`（`lib/tools/produce.py` 家族，与 produce_video/
  produce_report 同缝）：输入主题/文档/大纲 + 风格要求 + 页数（可选）+
  参考模板（可选）。
- 骑 `lib/production/` 基座（ProductionRuntime + jobs + stages checkpoint），
  复用通用 `/api/v1/tasks/*` 轮询/中止，零专属路由（charter 纪律）。
- 对话内交付：任务卡 → 完成后产物卡（artifacts）：**PPTX 成品** +
  每页预览 PNG 栅格 + PPTD 工程目录。迭代编辑 = 后续消息指定页码与改法，
  重跑单页 author→render→export（checkpoint 只重做受影响页）。

### 4.2 DSL：PPTD 子集（v1）

采用 PPTD 的文件组织与核心语义，裁剪为可自研渲染的子集：

```
deck/
  deck.pptd      # version/title/size(默认 [1280,720])/theme(colors/fonts)/pages[]
  pages/*.page   # 每页：background + elements[]
  media/*        # 本地媒体（相对路径，禁越界）
```

| 元素 | v1 支持 | 说明 |
|---|---|---|
| text | ✅ | 富文本 run（b/i/u/span style）、主题字号阶梯、行距/字距/对齐 |
| shape | ✅ | 内置形状（rect/roundRect/ellipse/箭头族/线条族）+ 纯色/渐变填充 + 描边/阴影 |
| line | ✅ | 直线/肘形/贝塞尔（端点+控制点） |
| image | ✅ | fit(cover/contain/fill)/crop/圆角/蒙版矩形；本地+远程 URL（下载入资产库） |
| icon | ✅ | 内联 SVG 直嵌（复用 icons 纪律：禁 emoji） |
| table | ✅ | 基础网格 + 首行/末行样式 + 单元格富文本 |
| chart | ⚠️ 光栅化 | v1 经 headless Chrome（内联 SVG/echarts 离线包）渲成 PNG 入 PPTX；v2 评估 python-pptx 原生 chart |

校验器零 LLM（`lib/slides/pptd/validate.py`）：必填字段、类型、bounds 不出界、
主题 token 可解析、媒体存在且禁越界（复用参考仓的路径安全规则：拒绝绝对路径
与 `..`）。**schema 校验失败 = 作者阶段内修复，绝不到渲染期**。

调用方给入的本地参考图也必须先收编进 `deck/media/`：公共配方边界把相对
`workdir` 归一为绝对路径，将存在的本地图片按内容散列复制成 deck-relative
引用；缺失或不支持的文件进入 `asset_findings`，不得把宿主绝对路径交给页作者。
这样预览、视觉 QA 与 PPTX 导出读取的是同一份自包含资产，而不是各自猜路径。

### 4.3 渲染器 `lib/slides/render.py`（PPTD → 预览图）

- PPTD 元素 → HTML/CSS 确定性映射（1280×720 画布，截图按 2x 出 2560×1440）。
- 与 motion_video 复用同一 headless Chrome 环境（`_env.py`）与字体/资产通道；
  渲染输出三用途：对话预览栅格、视觉质检输入、 chart/复杂效果光栅化源。
- 该渲染器是「预览保真」的单一事实源——**所见即所导出的对照基准**。

### 4.4 导出器 `lib/slides/export_pptx.py`（PPTD → PPTX，自研）

基于 python-pptx（已装 1.0.2）的原生可编辑写出器：

| PPTD | OOXML（python-pptx） |
|---|---|
| text | `add_textbox` + runs（字体/字号/颜色/粗斜/行距/字距/对齐逐 run 映射） |
| shape | `MSO_SHAPE` 自选图形 + fill(solid/gradient)/line/shadow |
| image | `add_picture`（fit/crop → crop_l/t/r/b） |
| table | `add_table` + 单元格样式映射 |
| line | connector / freeform |
| icon(SVG) | 转 PNG（cairosvg 或 Chrome 光栅）后 add_picture |
| chart | v1 = 光栅 PNG；v2 = `add_chart` 原生 |

- 坐标：PPTD px=pt → EMU（×12700）；页面 1280×720pt = 17.78×10in 16:9
  （或按 960×540pt=13.33×7.5in，导出尺寸随 deck.size）。
- 切换动画：默认淡入淡出——导出后 zip/XML 后处理写入
  `<p:transition><p:fade/>`，并按 CT_Slide 子序校验（手法见 §2.6）。
- 字体嵌入：按**实际文字 run × 实际样式槽**收集字符，只为真正用到的
  regular/bold/italic/boldItalic 写 fntdata；每个字体用 FontTools 做 Unicode +
  GSUB/GPOS 闭包子集，保留多语言 name 表与 fsType 权限，未用的主题/default
  字体不进入包。只有一个源字重但实际按粗体使用时，同一最近源写入 bold 槽，
  避免“字体已嵌入但 PowerPoint 仍报缺粗体”。`fsType` 禁止子集时保留全字体，
  禁止轮廓嵌入时明确跳过。
- 导出验收：ZIP 完整性 + presentation/slide XML 可解析 + slide 数 + transition
  校验 + fntdata 字形/槽位检查 + 用 DJI 金样做
  「渲染图 vs PowerPoint 打开截图」人工抽审。

### 4.5 Recipe 阶段图（`lib/slides/recipe.py`）

```
research(带 URL 的事实卡；不可用时明确 degraded)
  → outline(分页大纲+每页读者任务+来源 ID+版式/素材计划)
  → design(定场景→设计圣经+主题 token+字体对)
  → asset_preflight(主视觉图生并发+断点缓存，返回真实 deck 相对路径)
  → author_pages(整套创意合同→逐页 LLM 写 .page；schema/素材使用校验内环；
    失败页降级=结构最简单的标题+正文页，永不失败整部)
  → assets(远程图下载并本地化)
  → render(整页预览 PNG)
  → layout_qa(真实字形行框的溢出/碰撞检查→当前页最小修复→复测)
  → visual_qa(单页清单 + 整套带页码接触表；可定位 findings→单页修复)
  → export(PPTX) → deliver(artifacts 登记)
```

`research` 不是把用户的创意描述原样搜一次：它并发运行「近一月最新状态」、
「官方来源候选」和「背景材料」三条互补车道，卡片保留 `published_at`、
`query_lanes` 与整次研究的 `as_of`。`outline` 必须优先处理较新、真正第一方的
证据，明确区分预售价、最终售价、传闻和估算；零 LLM gate 会拒绝完全未引用
最新状态卡、遗漏已公布价格，或用“尚未公布”与新证据直接冲突的大纲。视觉 VLM
只负责画面与素材语义，不能代替这条事实 QA 车道。金额信号另按独立 host 聚合，
把「至少两站一致」与「单站摘要」分别送给大纲模型；精确数字须第一方或跨站一致，
不能把一个二手摘要（例如 29.9 vs 29.99）静默修圆；预计/网传/猜测价格以及
订金、意向金、优惠、补贴等附属金额不会进入价格共识。官方候选车道允许一跳
深挖并优先保留 URL 含产品专名的页面，但这些只算候选提示，绝不自动盖章官方。
事实 gate 的拒绝理由会注入
下一次 outline retry，让重试执行明确修订而不是随机重抽。

研究 checkpoint 使用 production evidence schema 版本和 6 小时共用 TTL，再按
`month` profile 标识。版本变化或超时会原子失效 research
及其全部下游 checkpoint，随后整条依赖后缀重跑，避免“新研究 + 旧大纲/页面/导出”
混成一次产物；TTL 内的进程崩溃仍按原 checkpoint 恢复。

`outline` 产物不是孤立页面数组：每页固定携带 `narrative_role`、
`layout_archetype`、`density`、相邻页信息与 `asset_mode`。页作者仍按页隔离以控制
成本/失败半径，但提示内含整套上下文；连续内容页的版式原型不得重复。主视觉义务
在作者运行前物化，页面 YAML 若不引用已生成路径则继续修复，不能“生成了等于没用”。

每阶段 checkpoint；`author_pages` 按页可重入。成本护栏：页数上限
（默认 ≤20）、QA 轮上限、图像生成配额——与 motion_video 的拍板同型。

### 4.6 对话界面

- 任务卡复用既有 production 任务卡渲染（progress/阶段事件走 push 通道）。
- 完成后消息内嵌产物卡：预览栅格（每页 PNG 缩略图，点击放大）+
  「下载 PPTX」+「工程目录」。前端走既有 artifacts 展示路径，
  api.js 单缝纪律不变。
- 迭代：用户在对话里说「第 3 页换成深色底」→ 命中同一 job 的 checkpoint，
  仅重 author 第 3 页 → 重 render 该页 → 重 export，秒级回新 PPTX。

---

## 5. Epic A′：motion_video 质量升级（消费 §3 共享层）

在不动渲染管线的前提下插四针：

1. **字体库接入**：`_fonts.py` 的单字体常量 → 字体注册表查询；recipe 的
   script 阶段输出 `theme.font_pair`，场景作者 prompt 注入对应
   `@font-face` 片段（资产通道已支持字体格式）。
2. **每片设计圣经 + 主题 token**：`_recipe.py` 增加 design 微阶段
   （场景归类 → 选圣经 → 定 palette/fonts/accent），`_scene_author` 的
   系统提示注入同一份主题块；模板 fallback 的 4 渐变轮播改为读主题块。
3. **视觉质检阶段**：compose 之后、render 之前，对每场景合成图取
   t=入场完成 帧截图 → §3.3 QA → findings 进作者修复循环（每场景 ≤2 轮，
   总 token 预算并入现有 per-scene budget）。
4. **真实影像通道**：场景 assets 简表（`subject`/`diagram` role）新增
   「图搜」来源——web 搜图 → 下载 → 资产库 → materialise（许可过滤：
   仅可取用来源；搜不到时回退 image_gen）。`_quality.py` 资产地板不变，
   只是达成手段多了真照片。

**明确不做的统一**（评审后否决项）：把 motion_video 场景也改成 PPTD 授权。
PPTD 是静态版式语言，没有时间轴/GSAP 动效语义；强融会两头失真。两者共享
字体/圣经/QA/资产/浏览器五层，不共享授权语言。

---

## 6. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| P1 | §3.1 字体库 + §3.2 圣经/主题 + motion_video 注入（§5.1-5.2） | 同一主题 A/B 出片对照；字体注册表测试（许可存证齐）；既有 motion 测试全绿 |
| P2 | §3.3 视觉质检共享阶段（video 先接入） | 金样片逐场景 QA findings 落 job.json；降级路径（无多模态）实测；防退化/预算测试 |
| P3 | slides v1：DSL+校验器+渲染器+导出器+produce_slides+对话产物卡 | DJI 金样 deck 过渲染器/导出器；自产 deck 在 PowerPoint/WPS 打开人工验收；stage checkpoint 杀进程重入实测；API 信封契约测试 |
| P4 | 原生 chart、PPTX→PPTD 导入（模板复刻）、按实际字形/样式槽子集嵌入、确定性文字碰撞闸、对话编辑回路打磨 | PPTX 可回读；子集字符覆盖；未用字体不进包；金样 layout findings=0 |

**风险登记：**
- 字体许可证是 P1 硬门槛——核验不过的字体宁可缺位。
- 导出器保真度（渐变/阴影/字距在 python-pptx 的映射损耗）以 DJI 金样
  逐元素对拍收敛；chart 光栅化是 v1 的务实妥协，必须在产物说明里明示
  「图表为图片」。
- 视觉质检的 VLM 调用成本：每页 1 图 1 调用，默认上限 20 页 × 2 轮，
  进 wallet 计价（与 motion_video 同型）。
