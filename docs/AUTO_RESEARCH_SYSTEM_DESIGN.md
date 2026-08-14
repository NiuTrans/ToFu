# Tofu 自动科研系统设计稿(Auto-Research System)——「一句话方向 → 可投稿的论文雏形」

> 状态:**R1–R4 已落地,R5–R7 仍为设计稿**。2026-07-26 落笔,2026-08-10 更新真实运行审计。
> 已落地范围见 §6 表格的 ✅ 行;**「已落地」= 代码 + 单测 + 可达性接缝测试全绿**,
> **不等于**「产出质量已经稳定」——真实跑已暴露过整批结构闸误杀、并发重复启动与 corpus
> folder 漂移,见 §6 末尾的诚实备注。
> 作者视角:在完整盘点现有底盘后写成 —— 本稿的第一原则是**组合已有件,不造新轮子**。
> 前置阅读:`docs/PRODUCTION_PIPELINE_DESIGN.md`(产出底盘/配方/知识包三层模型)、
> `lib/paper/` 现有引擎(report / recommend / insight / citation_audit / terminology_audit)。

---

## 0. 结论先行(先读这一页)

1. **这不是一个新子系统,是产出底盘上的「第四个配方」**。视频、播客、长报告已经证明了
   「一句话 → 成品」的三层形态(底盘 / 配方 / 知识包)。自动科研 = 一个更长、更重的配方:
   `方向发现 → 相关工作详查 → 创新点发现 → 实验/分析设计 → 图表生成 → LaTeX 装配 → Overleaf 编译`。
   它天然骑现有的 `lib/production/`(阶段图 + 崩溃续跑 + ProductionRuntime),**不新造生命周期机制**。

2. **本地论文库不用新建,现有的 `paper_library` + `phash` 就是 parse-once 缓存**。一篇论文按
   内容哈希(`sha256(text.strip())[:32]`)唯一寻址,解析文本存 `paper_library.parsed_text`、
   图存 `PAPER_IMG_DIR/<phash>/manifest.json`、报告存 `paper_reports(paper_hash, lang)`。
   **同一篇论文永不二次解析**这件事,库里已经做到了 —— 我们要补的只是「批量爬 N 篇并逐篇入库」这一层,
   它今天缺(recommend_engine 只推荐、不解析)。

3. **反「A+B 缝合」是本系统的智识核心,必须做成一道可度量的闸,不是一句 prompt 口号**。
   我们已经有 `insight_engine` 的**四轴 rubric 打分器**和**接地(grounding)机制**做范本 ——
   创新点也照此做:每个 idea 过一道**新颖性 + 可证伪性 + 机理深度**的评分闸,低分直接淘汰,
   而不是让模型自我表扬。

4. **图表能力今天只有两条真路,LaTeX 编译在 Overleaf 服务器上而非本机**。实测:本机有
   `matplotlib 3.6`(可直出 SVG/PDF/PGF 矢量)+ `graphviz` + Playwright(HTML/SVG→矢量 PDF),
   **但没有 pdflatex / tectonic / chromium 在 PATH 上**。所以:数据图走 matplotlib,示意图走
   HTML/SVG→Playwright,**TeX 编译一律交给 Overleaf MCP(`compile_project`)**,不在本机装 texlive。

5. **「自动科研」最大的诚实边界:它产出的是「雏形 + 证据链」,不是「已验证的科学发现」**。
   系统能保证的是:方向有依据、相关工作查得尽、创新点新颖且自洽、图表准确、格式可投稿。
   它**不能**替你跑真实实验、不能保证 idea 一定 work。把它定位成「资深博士生的第一稿」,
   而不是「导师签字的定稿」—— 这条定位决定了每个阶段都以**可介入、可否决、留证据**为准绳。

---

## 1. 现状体检(全部盘上核实,给出可复用的确切缝)

> 这一节是本设计能落地的地基:**每个需求都已有一半实现**,列清楚才能只写增量。

### 1.1 已经有的(直接骑,零重写)

| 需求侧 | 现成件 | 位置 | 复用方式 |
|---|---|---|---|
| 长任务生命周期 | `ProductionRuntime` + `stages.py` + `jobs.py` | `lib/production/` | 新配方 runtime 层实测 ~8 行;阶段图崩溃续跑是**正确性契约** |
| 配方骨架范本 | 长报告配方(512 行,阶段列表数据驱动) | `lib/longform/recipe.py` | 直接照抄结构:`research → outline → sections×N → assemble` |
| 联网调研 + 接地 | `run_agent_loop` + `_REPORT_TOOLS`(多查询 web_search + fetch_url)+ `_execute_report_tool` | `lib/paper/report_engine`、`lib/agent_loop.py` | 相关工作详查的**发现引擎原样可用**;研究调用方用 token 包络 + 重复工具模式强制收束,不改全局 Agent loop |
| 描述 → 真实 arXiv 论文 | `recommend_papers` / `iter_recommend_events` + **anti-hallucination grounding** | `lib/paper/recommend_engine/` | 每个候选必须经 `search_arxiv`/`fetch_arxiv_title` 接地才surface —— 防幻觉引用的现成闸 |
| 论文内容哈希 + 去重 | `_paper_hash` / `resolve_paper_hash` | `lib/paper/hashing.py` | `phash` 是全库唯一寻址键 |
| parse-once 缓存三件套 | `paper_library`(parsed_text/images)+ `PAPER_IMG_DIR/<phash>/manifest.json` + `paper_reports(phash,lang)` | `lib/paper/library.py`、`lib/paper/images/` | 命中即跳过重解析(manifest 版本匹配) |
| PDF 解析 | `parse_pdf` → text/images/math/VLM | `lib/pdf_parser/` | 逐篇解析一次,产物入库 |
| 洞察二次过(transfer moat)| `insight_engine`:reader-context(库+记忆)+ 四轴 rubric + grounding + headroom 闸 | `lib/paper/insight_engine/` | **创新点发现的直接范本** |
| 引用真伪审计 | `build_citation_audit`(零 LLM,CrossRef/arXiv 核验) | `lib/paper/citation_audit.py` | 论文成稿的引用不落空 |
| 术语自包含审计 | `build_terminology_audit`(零 LLM) | `lib/paper/terminology_audit/` | 成稿术语闸的范本 |
| 数据图 | **matplotlib 3.6**(直出 SVG/PDF/**PGF** 矢量) | 已安装 | 发表级数据图首选,LaTeX 原生 |
| 示意图 | Playwright HTML/SVG → **矢量 PDF** | `lib/artifacts/pdf_export.py` + `tofu_search` 的 headless 池 | 定制示意图/架构图 |
| LaTeX 全流程 | Overleaf MCP:`create/read/edit/upload_file` + `compile_project` + `download_pdf` + `download_log` | MCP 工具面 | **编译在 Overleaf 服务器**,本机无需 texlive |
| 记忆/检索 | BM25 prefetch(name+desc+body) | `lib/memory/` | 库内检索先用 BM25;需要语义近邻时另议(见 §7 风险) |

### 1.2 今天缺的(这就是要写的增量,清单收敛)

| 缺口 | 为什么缺 | 归属阶段 |
|---|---|---|
| **趋势/机构选题层** | 无任何「扫最近热点 + 大机构工作 → 高价值问题」的路径 | 阶段 1 `discover` |
| **批量爬 + 逐篇入库** | recommend_engine 只推荐、从不 `parse_pdf`;无「爬 N 篇 → 解析 → 建库」循环 | 阶段 2 `harvest` |
| **跨论文综述合成** | report_engine 是单篇;无多篇 fan-in 的 related-work 合成 | 阶段 3 `survey` |
| **创新点发现 + 反缝合闸** | insight_engine 面向「已读单篇」,非「面向空白造 idea」;无新颖性打分闸 | 阶段 4 `ideate` |
| **实验/分析设计** | 无「给定 idea → 可执行验证方案」 | 阶段 5 `plan_study` |
| **图表自动生成配方** | matplotlib/Playwright 是原语,无「idea/数据 → 成图」的编排 | 阶段 6 `figures` |
| **LaTeX 装配 + Overleaf 回环** | 无「markdown 雏形 → .tex 工程 → 编译 → 读日志修错」闭环 | 阶段 7 `typeset` |

**一句话**:底盘、调研引擎、论文库、接地机制、审计闸、图表原语、Overleaf 通道 —— 七块地基**全在**;
要写的是把它们串成研究配方的**七个阶段**,外加两个新原语(批量入库、新颖性闸)。

---

## 2. 系统总览:研究配方的七阶段

沿用底盘的阶段图契约 `Stage(name, run, gate, retry, resumable)`,每阶段产物落盘即 checkpoint,
崩溃从第一个未完成阶段续跑。**数据驱动的阶段列表**(相关工作有几篇就有几个解析子阶段)已被长报告配方
验证可行,直接复用同一「两趟跑图、共享一个 checkpoint」的手法。

```
用户:「帮我在『长上下文 KV-cache 压缩』方向找个能投 NeurIPS 的题」
        │
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ 阶段1 discover   趋势+机构扫描 → 候选问题集(短期/长期,各带依据URL)         │  LLM+web,可介入
│ 阶段2 harvest    每个候选 → 爬相关论文 → 逐篇 parse_pdf → 入 paper_library   │  批量,parse-once
│ 阶段3 survey     多篇 fan-in → 结构化综述 + 空白地图(谁做了什么/没做什么)    │  LLM+web,复用report引擎
│ 阶段4 ideate     空白地图 → 创新点候选 → 新颖性/可证伪性/机理深度打分闸       │  LLM+闸,反A+B核心
│ 阶段5 plan_study 选中idea → 实验或分析设计(可执行、有baseline、有度量)       │  LLM
│ 阶段6 figures    概念/数据 → matplotlib数据图 + HTML/SVG示意图(矢量)         │  代码执行+Playwright
│ 阶段7 typeset    以上全部 → .tex 工程 → Overleaf编译 → 读日志自修 → PDF       │  Overleaf MCP回环
└─────────────────────────────────────────────────────────────────────────┘
        │
        ▼
产物:①方向报告 ②本地论文库(可复用)③综述+空白地图 ④评分过的创新点 ⑤研究方案 ⑥矢量图 ⑦可编译LaTeX工程+PDF
```

**分析型任务的分支**:用户若要的是「有趣的分析型工作」而非「新方法」,阶段 4 的闸把
`idea.kind` 分成 `methodology` / `analysis` 两族,阶段 5/6 走不同模板(分析型重「现象度量 + 反直觉发现」,
方法型重「机制 + baseline 对比」)。**同一配方,两条产物形状**。

---

## 3. 逐阶段设计

### 阶段 1 — `discover`:趋势/机构选题(高价值问题发现)

**目标**:从「近期趋势 + 大机构工作」里,识别**短期可做(3–6 月出结果)**和
**长期高价值(方向性)**的问题,每条都挂真实依据。

**做法(复用 `run_agent_loop` + `_REPORT_TOOLS`,与 recommend_engine 同构)**:
- 多轮联网:扫 arXiv 最近(`freshness='month'`)+ 目标顶会/机构(DeepMind/FAIR/OpenAI/清北等)近期放出物;
- 注入 **current-date anchor**(recommend_engine 已有 `date_anchor_clause`),防模型拿旧年当未来;
- 每个候选问题产出结构:`{problem, why_valuable, horizon: short|long, evidence_urls:[...], maturity, competition}`。

**闸(零 LLM)**:
- 每个候选问题 **≥2 个真实 URL 依据**(照抄 longform 的 `_gate_research`「每条要点挂 URL」);
- `horizon` 与 `competition` 必填(逼模型区分「已挤爆的红海」和「有窗口的短期题」)。

**可介入**:候选问题集落盘后 job 默认继续,但用户可在气泡里拦下、圈定方向再放行
(复用现成 message-queue 抢占 + human_guidance,**不新造机制**)。

**类比**:像一个刚开完组会的资深博士生,先把「最近大家都在卷什么、哪些坑还没人跳」摸清楚,
再决定自己往哪挖 —— 而不是拍脑袋定题。

---

### 阶段 2 — `harvest`:批量爬 + 逐篇入库(本地论文库,parse-once)

**目标**:围绕选定问题,**尽可能穷尽**相关工作,爬进本地库,**每篇只解析一次**,后续所有阶段复用。

**这是两个新原语之一。做法**:
1. 用 `search_arxiv` + 阶段 3 的 agentic 搜索联合产出候选论文列表(去重按 `arxiv_id`);
2. 逐篇:下载 PDF → `parse_pdf` → 算 `phash` → **命中缓存则跳过**(库里有 `parsed_text` 且
   `PAPER_IMG_DIR/<phash>/manifest.json` 版本匹配),否则 `_persist_library_row` + 抽图建 manifest;
3. 库以 `phash` 去重,**天然共享**:用户此前在阅读模式读过的论文,这里直接命中不重解析,反之亦然。

**与阅读模式的融合(用户明确要求「降低开销」)**:
- **同一张 `paper_library` 表、同一套 `phash`**,所以自动科研爬进来的库,用户能在阅读模式里直接翻、直接问答;
- 反过来,用户读过、生成过报告的论文(`paper_reports` 有行),综述阶段**直接引用已有报告**而非重新读;
- `folder_id` 给自动科研任务开一个专属文件夹,与用户手动库隔而不离。

**闸(零 LLM)**:入库数 ≥ 阈值(如 ≥15 篇)且解析成功率 ≥ 阈值;每篇 `parsed_text` 非空。

**成本纪律**:parse-once 是关键。一次 `harvest` 若爬 40 篇,命中已读的 10 篇零成本,
只解析新的 30 篇;**下次换个近邻方向,这 40 篇又是零成本命中**。库越用越省。

**缓存失效契约(v3,2026-07-27 owner 拍板,已落地)**:「parse once, reuse forever」的缓存键是
**`(phash, parser_version)`**,不是裸 `phash`。每个写 `parsed_text` 的写者(harvest 的
`_persist_row`、阅读模式服务端落库、bookshelf PUT 保留语义)都给行打上**产出该文本的抽取器身份+版本**
(如 `pymupdf4llm-1.27.2.3`;raw 降级路径打 `pymupdf-raw-…`)。harvest 预下载探针要求该版本与本环境
`expected_parser_version()` **精确相等**才命中。于是:①解析栈升级 → 旧版本行自然 miss → 重解析,
不会把陈旧文本冻结一辈子;②markdown 管线不可用时的 raw 降级写入,版本戳与期望不同 → **永远不算
markdown 语料命中**,下次 harvest 自动重解析自愈;③契约前写入的存量行 `parser_version=''` → 按构造
miss → 下次触碰即自愈,**无需任何回填脚本**。降级写入同时是**响的**:ERROR 日志 + `audit_log` +
`HarvestResult.degraded` + 批次计数,而不是埋进一行 warning。安装器现把 PyMuPDF 三件套精确锁到
同一版本,并运行 `scripts/verify_pdf_stack.py` 的真实 Markdown smoke,不会再把「包名存在」误当成
「解析栈可用」。教训来源:旧环境的模块名/版本分裂曾令 `pymupdf4llm` 静默降级为无结构裸文本,
而非空 `parsed_text` 又被无条件命中 —— 若无版本键,整个语料库会被永久污染。

**类比**:建自己的「文献仓库」而不是每次读论文都从头翻 PDF —— 仓库里已经上架的书,再借不要钱。

---

### 阶段 3 — `survey`:多篇 fan-in 综述 + 空白地图(防重复的核心)

**目标**:把 `harvest` 的库合成一份**结构化综述**,并输出**空白地图 `open_gaps.json`**(谁做了什么、
用了什么假设、留了什么没做)—— 这张图是阶段 4 造 idea 的原料,也是「不与已有工作重复」的**证据**。
**它是可机读的 R3 输入契约,不是只给人看的综述附庸。**

**做法(复用 report_engine 的 loop,做多篇版)**:
- `messages` 里塞 N 篇的**已有报告/摘要/方法卡**(不是全文,控 token);仍给 web 工具做交叉核对;
**输入纪律(降开销的落点,R2 pin #2)**:survey 喂给模型的每篇论文,**优先用 `paper_reports`
里已生成的报告**(有则零成本引用),其次用 `paper_library.parsed_text` 的截断摘要;**绝不**对已入库
论文重新 `parse_pdf` 或重新生成单篇报告。每篇输入按篇数硬上限截断(`_SURVEY_PER_PAPER_CHARS`),
避免 N 篇全文塞爆 token。

**做法(复用 report_engine 的 loop,做多篇版)**:
- `messages` 里塞 N 篇的**已有报告/parsed_text 摘要**(不是全文,控 token);仍给 web 工具做交叉核对;
- 产物两份:①人读的综述 markdown;②机器读的**`open_gaps.json`**(下方 schema **已定稿**,是 R3 的输入契约)。

#### `open_gaps.json` — 冻结 schema(R3 反 A+B 闸的输入契约,R2 pin #1)

> 这个 JSON 是 R3 的**唯一**结构化输入。R3 的新颖性闸拿 `open_gaps[].id` 当「这个 idea 是否解决一个
> 真实空白」的判据,所以字段名和形状在此**定死**,R3 不得再改。schema 版本用 `schema_version` 显式标注,
> 将来演进走版本号而非静默改形。

```jsonc
{
  "schema_version": 1,
  "direction": "长上下文 KV-cache 压缩",          // 本次 survey 的方向(原样回填)
  "lang": "zh",
  "surveyed_count": 18,                            // 实际纳入综述的库内论文数(诚实覆盖面)
  "library_folder_id": "research_<task>",          // 这批论文所在的库文件夹(可回查)

  "clusters": [                                    // 把相关工作按主题聚类
    {
      "id": "cluster_1",
      "theme": "训练时 KV 低秩投影",
      "papers": ["2305.xxxxx", "2401.xxxxx"],      // ★ 每个 id 必须在 paper_library 查得到(结构闸)
      "shared_assumption": "注意力矩阵低秩",         // 这簇共同的前提假设
      "limitation": "推理时才压、无法端到端学",       // 这簇的共同局限
      "unexplored": ["把压缩率做成可学习的每层变量"]   // 这簇没碰的方向
    }
  ],

  "method_matrix": [                               // 论文×维度矩阵(每行一篇库内论文)
    {
      "paper": "2305.xxxxx",                       // ★ 必须在 paper_library 查得到(结构闸)
      "task": "长文摘要",
      "assumption": "低秩",
      "data_scale": "16k ctx",
      "metric": "ROUGE / 显存占用",
      "open_source": true
    }
  ],

  "open_gaps": [                                   // ★★ R3 的直接原料:真实空白清单
    {
      "id": "gap_1",
      "gap": "没有方法在压缩时保住 needle-in-haystack 的精确检索",
      "why_open": "现有工作只测困惑度,不测长程精确召回",
      "evidence": ["2305.xxxxx", "2401.xxxxx"],    // ★ 每个 id 必须在 paper_library 查得到(结构闸)
      "kind_hint": "methodology"                   // methodology|analysis — 给 R3 分流的软提示
    }
  ]
}
```

**闸(两道,都零 LLM):**

1. **库内可查证结构闸(R2 pin #1 的核心,新写 `_verify_against_library`)**:`clusters[].papers`、
   `method_matrix[].paper`、`open_gaps[].evidence` 里的**每个 arxiv_id**,都必须能在本次 harvest 建的
   `paper_library`(按 `library_folder_id` 或全库)里按 `arxiv_id` 查到。**查不到 → 该 id 从该条目里
   剥掉**(recommend grounding 反着用:不是接地新引用,是校验「综述声称覆盖的论文」确实已入库);
   若一个 `open_gap` 的 `evidence` 被剥空 → **整条 gap 判为无据、丢弃**(默认按「模型编的」处理,而非
   「真漏爬」—— 漏爬走一条独立的补 harvest 信号,不混进这里)。剥离动作记 `stripped_ids` 到产物 meta,
   可复盘。
2. **引用真伪闸(复用 `build_citation_audit`)**:综述 markdown 里每个 `arXiv:<id>` 过
   `build_citation_audit` 核验;出现可疑(不可解析)引用 → 产物 meta 带 citation-integrity 卡标红。

**类比**:不只写「别人做了啥」的流水账,而是画一张**战场地图**:标出每支队伍的阵地和补给线,
最重要的是标出**没人占的高地**。而那道库内可查证闸,就是**核对地图上标的每支队伍确实在你的情报库里
有档案** —— 地图上凭空多出一支查无此人的队伍,直接抹掉,免得 R3 拿它当真去规划。

#### R2/R3 接缝修订(v2,2026-07-27 真跑暴露后拍板)——「库内校验闸 vs harvest 覆盖率」的三档接地

> **真跑发现的张力**:上面 v1 的库内校验闸要求 `evidence` 的每个 id **已入库**(harvest 成功解析过)。
> 但真跑实证:LLM 综述会引用**存在但未入库**的近邻(harvest 只入成功解析的篇,或综述提到库外相关工作)。
> v1 把这些 gap 全丢弃 → 0 gaps → 链死。根因不是网络,是**「已入库」这个判据本身太强**。
> 修订原则:放宽覆盖率**不能偷偷降低「基于真实读过的论文」这个地基**。

**① evidence id 三档接地(取代 v1 的二元「在库/不在库」):** `_verify_against_library` 对每个
`clusters[].papers` / `method_matrix[].paper` / `open_gaps[].evidence` 的 id 判定三档 —

| 档 | 判定 | 处理 |
|---|---|---|
| **`library`**(最强) | 在 `paper_library`(按 `folder_id`/全库)查到 | 保留,是「真读过」的证据 |
| **`grounded`**(中) | 不在库,但 `fetch_arxiv_title` 能确认真实存在 | 保留,标 `grounded`,**id 加进 `missing_ids`**(下轮 harvest 自动补——接上 R1 已有语义) |
| **剥掉**(幻觉) | 既不在库、也接不上 | 从条目剥掉(维持 v1 行为) |

一个 `open_gap` **只有 evidence 全部落到「剥掉」档(全幻觉)才丢弃**;有任一 `library` 或 `grounded`
存活即保留。这样既不放过缝合怪的幻觉引用,又不因 harvest 没抓全而误杀真实空白。

**② `low_confidence` 防后门(关键,防止放宽变成绕过 R3 地基的后门):** 一个 gap 若其 evidence 里
**`library` 档为 0**(纯靠 `grounded` 撑着),标 `low_confidence: true` 并附
`library_evidence_count` / `grounded_evidence_count`。语义:这个空白「基于我们**没真正读过**的论文」,
R3 判新颖性时不能全信。

**每个 gap 的新字段(schema v1 增补,向后兼容——旧读者忽略新键):**
```jsonc
{ "id": "gap_1", "gap": "...", "why_open": "...",
  "evidence": ["2305.xxxxx", "2401.yyyyy"],      // 保留档(library + grounded)
  "evidence_tiers": {"2305.xxxxx": "library", "2401.yyyyy": "grounded"},
  "library_evidence_count": 1, "grounded_evidence_count": 1,
  "low_confidence": false,                        // true 当 library_evidence_count == 0
  "kind_hint": "methodology" }
```

**③ R3 消费 `low_confidence`(反 A+B 闸的地基保护):** `ideate.generate_ideas` 判一个 idea 时,
若其 `linked_gap_id` 指向的 gap 是 `low_confidence`:
- idea 的 rubric `value` 轴**扣一档**(该 gap 建立在未读论文上,「是否真解决真实空白」的置信度低);
- 结果记 `linked_gap_low_confidence: true` + `补 harvest 建议`(用 gap 的 `missing_ids`),让 owner
  可选择「补 harvest 后重判」而非直接采信。
这条是硬约束:**放宽 survey 覆盖率的代价,由 R3 显式承担并标注,不静默传导。**

**④ harvest 覆盖率补强(R1 侧,治本):** `harvest_arxiv_id` 对**瞬时**下载/解析失败**重试一次**
(瞬时失败不该永久丢一篇);`_MIN_HARVEST_PAPERS` 与 `_DEFAULT_HARVEST_N` 拉开(默认 harvest_n
提到能稳定拿到 ≥ 下限的量)。这样「grounded 但未入库」的比例从源头压低,`low_confidence` gap 变少。

**零 LLM 纪律**:三档判定里 `library` 是纯 DB 查(零成本),`grounded` 复用 `fetch_arxiv_title`
(已有重试+abs-page fallback);`low_confidence` 是纯计数。整条接缝**不新增一次 LLM 调用**。

---

### 阶段 4 — `ideate`:创新点发现 + 反「A+B」新颖性闸(智识核心)

**目标**:从空白地图产出**真正有价值的突破点**,方法型或分析型皆可,**并用一道可度量的闸淘汰缝合怪**。

这是本系统最难、也最区别于「随便让 LLM 想 idea」的地方。分三步:

**第一步 — 发散生成(复用 insight_engine 的高温 + transfer moat)**:
- 温度略高(insight_engine 用 0.45,发散但不破坏 JSON);
- 注入 **reader-context**(库 + 记忆),让 idea 能建「这个技巧从 «你读过的X» 迁移到这个空白」的桥
  —— 迁移是 insight_engine 已验证的护城河,泛泛总结器造不出;
- **发散必须锚在 R2 的 `open_gaps` 上**:generator 收到 survey 冻结产出的 `open_gaps.json`,
  每个 idea **必须 `linked_gap_id` 指向其中一个真实空白**(不是凭空发明问题)。

#### idea schema — 冻结契约(R3 pin,对齐 R2 `open_gaps`)

```jsonc
{
  "id": "idea_1",
  "title": "每层可学习压缩率的 KV 低秩投影",
  "kind": "methodology",                  // methodology | analysis(必填,决定 R5/R6 模板)
  "linked_gap_id": "gap_1",               // ★必填:指向 R2 open_gaps[].id;指不到已校验空白 → 结构闸判无效
  "core_mechanism": "为什么会 work 的机理,一句话(不是操作步骤)",
  "novelty_claim": "相对哪些具体工作(arxiv_id)新在哪",
  "prior_art": ["2305.xxxxx", "2401.xxxxx"],  // 模型自报的近邻(★不是新颖性判据的全部,见 pin #1)
  "retrieval_query": "KV cache low-rank projection compression",  // 3-8 个术语，无标点/星号/整句
                                              // ★零额外 LLM 调用(随生成一并输出);缺失或不合法时
                                              // 走零 LLM 兔底构造器，两路均过同一个净化器
  "falsifiable_prediction": "一个能被实验证伪的具体预测",
  "why_not_AB": "为什么这不是两个已有件的简单拼接(必填,非空)"
}
```

**第二步 — 反 A+B 闸(三道,顺序执行,前两道零 LLM 先跑省成本)**:

**闸① 零 LLM 结构闸(免费,先跑)**:
- `linked_gap_id`、`why_not_AB`、`core_mechanism`、`falsifiable_prediction`、`kind` **必填非空**;
- **`linked_gap_id` 必须命中 R2 `open_gaps` 里一个真实 id** —— 指不到已校验空白的 idea 判无效
  (R2 pin #1 姿态的延续:没有证据的 idea = 缝合怪的温床,凭空发明的问题不算解决问题);
- `novelty_claim` 必须引用具体 `arxiv_id`(而非「据我所知没人做过」)。
- **NEUTER 就打在这道闸**:摘掉「必须 link open_gap」这一条 → 凭空造问题的缝合怪漏过 → 测试翻红。

**闸② 强制近邻检索的新颖性判定(R3 pin #1 的核心 —— 新颖性 = f(检索集合),不是 f(模型自报))**:
- **不允许** judge 只用 idea 自带的 `prior_art`。判定前**强制跑 `search_arxiv`**,把 top-K(K≥5)
  **无选择地**拉进检索集;
- **★ 送上线的查询必须是术语，不是散文(2026-07-27 实测后固定)**。原实现把
  `title + 整段 core_mechanism`(473-558 字符、带 `*星号*`/括号/逗号)直接当 `all:` 参数，
  导致 pin #1 **从未真正生效**。用生产作业 `research_7a444f96c65d42b5` 的 6 条真 idea 实测
  (每次都打真 arXiv API):

  | 指标 | 修前(HEAD) | 修后 |
  |---|---|---|
  | 领域命中 | **0/25 (0%)** —— 召回全是引力波/中微子/GWTC 协作组论文 | **24/25 (96%)** |
  | 距 idea 近邻集最大重合 | **80%** —— novelty 轴实际是常量 | **0%** |
  | 空基底 | **1/6**(idea 3 的 `*difference*` 触发 HTTP 500，被吞成 `[]`) | **0/6** |

- **两条查询构造路径，同一个净化器**:idea schema 的 `retrieval_query`(模型供，3-8 术语)
  与零 LLM 兔底构造器(从 title 抽术语)**都过 `sanitize_retrieval_query`**。硬判据:
  任何 idea 都不得把散文原样送进检索 API。
- **字段化查询 + 渐宽阶梅(实测得出，两个语法坑都踩过)**:单纯净化修好了相关性却
  治不了区分度 —— 两个标题都以 "KV Cache Compression" 结尾的 idea 仍得到**字节相同**的近邻集，
  因为 `all:` 是无序词袋。改成 `(ti:a OR ti:b) AND all:"<domain>"`,其中 identity/domain 的划分
  是**按批次计算**的结构判据(多条 idea 共享的术语 = 领域背景，独有的 = 该 idea 的身份)，
  不是手调权重。**两个必须避开的写法，均实测 0 召回**:①`ti:"a b"` 引号 = 精确短语匹配，
  而新颖 idea 的身份短语按定义不在任何已有标题里;②`ti:a AND ti:b` 要求每个身份词都在同一标题。
  因此 identity 用 **OR**。阶梅 = title → abstract → domain 短语 → 平 `all:`,**渐宽而非直接塔缩回词袋**:
  6 条真 idea 里有 2 条的身份术语真的新到没有任何标题带它，靠 abstract 腿才拿到基底，
  否则会谬报成「无先行工作」。
- judge 拿**这个检索集合**(而非模型挑的三篇)对比,显式回答:核心机制相对**检索到的最近那篇**,
  差异是 **mechanism-level 还是 parameter-level**;
- **parameter-level 增量(换数据集/换 backbone/两模块拼接)→ novelty 轴硬上限**(不让均分糊过去);
- **检索集为空 → `novelty_basis='none'`，该 idea 禁止进 `accepted`**。空基底上的高分不是新颖性证据，
  是一个没被审的 idea 穿了分数的马甲。**宁可判不了,不许假装判过**，整个 pass 带 degraded/gate_reached。
- **审计字段不得合并两种凭据**:`retrieved_ids`(真正被判的基底)与 `self_reported_ids`
  (模型自报)**分字段存放**。旧实现合成一个 `prior_set_ids` 并集，而某条真 idea 自报 257 个 id
  、检索只有 5 个 —— 审计记录于是声称了一个 41 篇的新颖性基底，而 judge 只看过 5 篇。
- **NEUTER 就打在这道闸**:一个 idea 的新颖性主张只在「不检索、只用自带 prior_art」时成立,
  一旦强制检索 top-K 拉进一篇明显撞车的近邻 → 闸把它毙掉。断言 judge 的 prior 集 ≥ K
  且**包含非模型自报的 id**。
- **守卡用真语料，不用 `_FakeSearch`**(教训):原有 17 个单测全绿却漏掉了这个缺陷，因为它们用
  `_FakeSearch` 注入固定近邻，**从未测过真实查询构造**(charter「守卫绿着空转」同族)。
  现在 `tests/test_paper_ideate_retrieval.py` 用那 6 条真 idea 作固定语料(落在
  `tests/fixtures/r3_real_ideas.json`，**跟踪入库**—— 读未跟踪的 `data/` 会让它在干净检出里
  全部 SKIP，那正是本 epic 要关的那个失效形态);语料缺失时**报错而非跳过**。

**②-bis 后端对照(tofu-search vs arXiv Atom)—— 首轮因基线损坏未能进行，判定推迟不取消**:
本次只修好了 arXiv 侧的查询构造。在修好之前做任何后端对照都是无意义的 —— 基线本身
(0/25 相关、近邻集恒定)是坏的，任何对手都会赢，而那只能证明基线坏、不能证明对手好。
现在基线已修复并有了可重算的数字(96% / 0% / 0空基底)，对照才有参系。
**本条明确不是「实测否决」** —— tofu-search 供底的 rerank 在语义相似度上仍可能优于
关键词匹配，只是尚未测。下轮用**同一组 6 条 idea + 同一套判据**(领域命中率下限 +
跟 idea 重合上限)跑对照，才能下结论。

**闸③ 四轴 rubric 打分(LLM-judge,照抄 insight `_rubric` 的 dispatch→parse→coerce→重算均值)**:

| 轴 | 问什么 | 低分即淘汰的信号 |
|---|---|---|
| **新颖性(相对检索集合)** | 与**检索到的**最近工作比,增量是机理级还是参数级? | 只是「换数据集/换 backbone/两模块拼一起」 |
| **可证伪性** | `falsifiable_prediction` 是否具体到能设计实验判真伪? | 空泛(「效果更好」),无法证伪 |
| **机理深度** | `core_mechanism` 是否解释了「为什么」,而非只描述「做什么」? | 只有操作步骤,没有原理 |
| **价值** | 若成立,是否真正解决 `linked_gap_id` 那个空白? | 解决的是不存在/已被解决的问题 |

- **接地闸(复用 recommend grounding)**:`prior_art` / `novelty_claim` 提到的每篇论文经
  `search_arxiv`/`fetch_arxiv_title` 接地;接不上的剥成 null,新颖性主张建立在幻觉论文上 → 判无效。
- **阈值 = 单个可调常量 `IDEATE_GATE_THRESHOLD`(初值 4.0,与 insight 齐平)**:四轴均分 < 阈值的
  idea 被淘汰。**宁缺毋滥**。阈值是常量不是硬编码在逻辑里 —— 调它不改行为、不翻测试。
- **淘汰留档(可复盘,owner 要求)**:每个被毙的 idea + 四轴分数 + 每道闸的淘汰理由**全部落档**
  (走 `paper_reports` 的 `ideate:<lang>` 复合键的 meta),用真数据校准阈值,而非拍脑袋定死。

**第三步 — 对抗式自检(可选增强)**:对存活的 idea 起一个 `reviewer` 角色的反方 agent,
专门找「这其实是 arxiv:XXXX 的换皮」的反例;找到即打回。这一步把「审稿人视角」前置。

**类比**:一个诚实的导师不会夸你「A+B 很有创意」。这道闸就是那个导师 —— 先逼你说清「最近谁做过类似的」,
再逼你回答「你比他到底新在哪个机理上」,答不上来的 idea 当场毙掉。**它宁可少给你一个 idea,
也不给你一个会被审稿人一眼看穿的缝合怪。**

---

### 阶段 5 — `plan_study`:实验/分析设计

**目标**:把选中的 idea 变成**可执行的验证方案**(方法型)或**分析方案**(分析型)。

- **方法型**:`{hypothesis, baselines:[具体方法+arxiv_id], datasets, metrics, ablations, expected_result, risk}`;
- **分析型**:`{phenomenon, measurement_protocol, controls, what_would_surprise_us, data_source}`;
- **闸**:baseline/对照必须是**真实且可获取**的(接地);度量必须客观可复现;`expected_result` 与
  阶段 4 的 `falsifiable_prediction` 一致(防止方案偷偷换了目标)。

**诚实边界**:本阶段**产出方案,不跑实验**。若将来接入 hope/MLP 训练平台(项目已有 MCP),
可增一个可选的 `run_study` 阶段真跑小规模验证 —— 但这是 v2,不在本稿承诺内。

---

### 阶段 6 — `figures`:自动生成发表级矢量图

**目标**:为综述、方案、创新点自动配**极致美观的科学图**。**实测约束决定分工**:

| 图种 | 工具 | 产物 | 为什么 |
|---|---|---|---|
| 数据图(曲线/柱/热力/散点) | **matplotlib**,统一样式表(字体、配色、DPI、去脊) | `.pdf` + `.pgf`(LaTeX 原生矢量) | 已装;PGF 让图里的字体与正文一致,发表级 |
| 概念/架构/流程示意图 | LLM 手写 **SVG/HTML** → Playwright `page.pdf` | `.pdf`(矢量) | `lib/artifacts/pdf_export.py` 现成 headless 通道;矢量、可精修 |
| 需要「好看的插画」(封面/示意人物) | `generate_image`(FRIDAY Gemini / OpenAI) | `.png`(位图) | 仅用于非数据、非严谨图;**绝不用于数据图**(扩散模型不保证数值准确) |

**质量纪律(呼应「极致美观」)**:
- 一张**统一的 matplotlib 样式表**(类似期刊模板:无顶右脊、色盲友好配色、衬线字体匹配 LaTeX);
- 示意图起**每图一个窄工具集的子 agent**(照抄 motion_video 的 `_scene_author` 模式:
  `run_agent_loop` + 只给 `write_file`/`fetch_url`/渲染检查),带反馈自修 1–2 轮,失败降级到模板图,
  **单张图失败不拖垮整篇**;
- **零 LLM 闸**:每张图产物文件存在、非空、能被 Playwright/matplotlib 成功渲染(渲染即验证);
  数据图的数据来源必须可追溯(不许模型编数据画图)。

**类比**:数据图像「用精密仪器画的工程图」(matplotlib,数值不骗人);示意图像「设计师手绘的示意」
(SVG,好看且可改);扩散生成的插画只配当「封面配图」,绝不进正文的实验结果。

---

### 阶段 7 — `typeset`:LaTeX 装配 + Overleaf 编译回环

**目标**:把前六阶段的产物装成一个**能编译的 LaTeX 工程**,并**自动修编译错误**直到出 PDF。

**做法(复用 Overleaf MCP,编译在其服务器,本机无需 texlive)**:
1. 选模板(会议 style:neurips/icml/acl…),用 `create_file` 建 `main.tex` + 章节 `.tex` + `refs.bib`;
2. 图:matplotlib 的 `.pgf`/`.pdf` 与示意图 `.pdf` 经 `upload_file`(二进制)传入工程 `figures/`;
3. `compile_project` → 若失败,`download_log` 读 LaTeX 日志 → 定位错误(缺包/未定义引用/图路径)→
   `edit_file` 修 → 重编。**这是一个有界的「编译-读日志-自修」agent 回环**(照抄 motion_video 的
   失败分类自修,上限 2–3 轮);
4. 成功后 `download_pdf` 落地,产物挂到气泡里可直接看。

**bib 纪律**:`refs.bib` 的每一条**从库里已接地的 arxiv_id 生成**(有真实 title/author),
再过 `citation_audit` 复核 —— **参考文献里不许有一篇编造的论文**。

**闸(零 LLM)**:`compile_project` 返回成功 + `download_pdf` 拿到非空 PDF;`\cite` 的每个 key 在
`refs.bib` 里存在(无 dangling cite,照抄 terminology_audit 的 dangling 检测思路)。

**类比**:像一个会自己看编译报错、自己查缺哪个宏包的排版助理 —— 你不用管 `! Undefined control sequence`,
它读日志、装包、重编,直到 PDF 出来。

---

## 4. 与产出底盘的接线(不新造机制)

| 底盘能力 | 本配方怎么用 |
|---|---|
| `ProductionRuntime` | 新 `lib/research/runtime.py`(实测 ~8–100 行);dedup key = `(方向, 会议, lang)` |
| `stages.py` 阶段图 | 七阶段 = 七个 `Stage`;`harvest` 的逐篇解析 + `figures` 的逐图生成用**数据驱动子阶段**(照抄 longform section×N) |
| `jobs.py` 崩溃续跑 | 已爬的论文、已生成的图、已编译的章节**永不重做**;进程死了从第一个未完成阶段续 |
| 通用任务端点 `/api/v1/tasks/*` | 零自建 poll/abort 路由(发现制已就绪) |
| 进度双投影 | 气泡内联生产卡(当前阶段中文名/第几篇/已生成图缩略图/中止)+ 面板;抄 `code_exec.py` 的 `_partialOutput` 断线重放 |
| 入口工具 | 一个语义明确的 `produce_research(direction=, venue=, lang=)`,门槛低到模型在完整 schema 上自然选中(照抄 `produce_video`) |
| 知识包(技能商店) | 一个 `research-director` 包承载:各会议风格、图表美学规范、「什么是好 idea」的判据 —— 由阶段 4/6 的子 agent 按需 `load_skill` |

**净增代码估算(按长报告配方 512 行的先例)**:配方业务 ~700–1000 行(七阶段比长报告的四阶段重)+
两个新原语(批量入库 ~150 行、新颖性闸 ~200 行,后者大量复用 insight rubric)+ runtime/入口 ~120 行。
**大头是 prompt 与闸,不是新基建。**

---

## 5. 数据与存储(全部复用现有表,不新增 schema)

| 产物 | 存哪 | 键 |
|---|---|---|
| 爬进来的论文(解析文本/图) | `paper_library`(现有) | `phash` / `(id, user_id)` |
| 逐篇报告(若生成) | `paper_reports`(现有) | `(paper_hash, lang)` |
| 综述 / 空白地图 / 创新点 / 方案 | `paper_reports` 用**复合 lang 键**(照抄 insight 的 `insight:<lang>` / review 的 `review:<venue>:<lang>`)| `(survey_phash, "survey:<lang>")` 等 |
| 图 | 磁盘 `jobs/<task>/figures/` + Overleaf 工程 | 路径引用 |
| job 清单(崩溃续跑) | `jobs/<task>/job.json`(底盘现成) | task_id |

**关键**:综述/创新点这类「合成产物」也走 `paper_reports` 的复合 lang 键,意味着它们**天然复用**
report 的 upsert 写路径、PG/SQLite 桥、缓存读 —— **一行新 schema 都不用加**。这正是 insight/review
两个先例已经趟平的路。

---

## 6. 分期实施(steady, step-by-step —— 每期独立可验收、可停)

> 顺序原则:**先补最缺、最独立、最高价值的前半段,再串重工具的后半段**。
> 每期一个 commit、精确 pathspec、失败先行 + NEUTER(项目既有纪律)。

| 期 | 范围 | 验收(客观可查) | 依赖 |
|---|---|---|---|
| ✅ **R1 — harvest 原语** | 批量爬 + 逐篇 `parse_pdf` + 入 `paper_library`(parse-once 命中跳过) | 给 20 个 arxiv_id,首次全解析入库,二次运行**零重解析**(命中缓存);阅读模式能翻到这些库论文 | 无(纯用现有库) |
| ✅ **R2 — survey 阶段** | 多篇 fan-in → 综述 markdown + 空白地图 JSON;citation_audit 接引用 | 给一个方向,产出综述,引用全部可解析(0 可疑);空白地图 JSON schema 合法 | R1 |
| ✅ **R3 — ideate + 新颖性闸** | 创新点生成 + 四轴 rubric 打分 + 零 LLM 结构闸 + grounding | 一批 idea 里,故意塞的「A+B 缝合」样本被闸淘汰(NEUTER:摘掉 `why_not_AB`/`prior_art` 闸 → 缝合怪漏过,测试翻红) | R2 + insight_engine |
| ✅ **R4 — 配方骨架上底盘** | 把 discover→…→ideate 串成 `lib/research/` 配方,骑 `ProductionRuntime`,崩溃续跑,`produce_research` 入口 | 崩溃后从未完成阶段续;已爬论文/已评分 idea 不重做;通用任务端点可 poll/abort | R1–R3 + `lib/production/` |
| **R5 — figures 阶段** | matplotlib 样式表 + 数据图;HTML/SVG→Playwright 示意图;每图子 agent 自修降级 | 一份方案自动出 ≥3 张矢量图,单张失败降级不拖垮整篇;数据可追溯 | R4 |
| **R6 — typeset + Overleaf 回环** | .tex 工程装配 + `compile_project` + 读日志自修 + `download_pdf`;bib 全接地 | 端到端:一个方向 → 出一份能编译的 LaTeX 工程 + PDF;无 dangling cite;refs.bib 0 编造 | R5 + Overleaf MCP |
| **R7 — discover + 前端生产卡 + 知识包** | 趋势/机构选题阶段;气泡生产卡双投影;`research-director` 技能包 | 给一句方向,零人工干预跑完七阶段;进度可见、可介入、断线重放 | R4–R6 |

**顺序理由**:R1(harvest)是所有阶段的燃料且完全独立,**先建库**;R3(反 A+B 闸)是智识核心且价值最高,
早做早验证「这系统到底会不会造缝合怪」;后半段(figures/typeset)重工具、依赖前半段产物,靠后。
discover 放最后是因为它最"锦上添花"—— 没有它用户手动给方向也能跑,有了它才「一句话出片」。

### R1–R4 落地后的诚实备注(别把绿测试读成「稳定且有价值」)

**已证实的**:四阶段的代码路径、零 LLM 闸、schema 契约、崩溃续跑钩子均有单测覆盖(35 个用例);
`produce_research` 的**四道可达性接缝**(工具 schema 进装配列表 / dispatch handler 已注册 / runtime 被
通用任务端点发现 / 启动时调用崩溃续跑)各有一道守卫,且 **4 道均经 NEUTER 验证会真红**。

**真实运行审计(2026-08-10 回看 `data/research/jobs/`)**:
- 同一方向留下 4 个 job:3 个 `error`,1 个 `done`;唯一完成的 run 读了 20 篇论文、产出一份可读综述,
  但 6/6 idea 因 `kind` 大小写/同义词被结构闸误杀,昂贵的新颖性检索与 rubric 根本没跑。
  所以该次 `done` **不能算有价值 insight 的成功样本**。后续已加入 kind 归一化和
  `gate_reached/degraded` 双轴,结构闸全灭不再伪装成 clean success。
- 4 个同方向 job 暴露了 dedup 的 check→create 竞态;共享论文又只有一个可变 `folder_id`,导致同一 corpus
  的行被并发 job 分散到不同 folder,完成 run 明明读过的证据却被降成 `grounded/low_confidence`。
  现已把 ProductionRuntime 的研究启动改为原子 claim,且 survey 以**实际加载的 arxiv id 集**作为
  library-grade 证据,不再把可变 folder 当 provenance。
- 旧产物没有逐调用 usage,无法回答「token 花在哪」;现 survey 全轮 + ideate 生成 + 每个 rubric 调用均走
  `lib.cost` 统一计价并分阶段持久化/展示。Agent 工具事件也复用 Reading Mode 的统一时间线。

**仍未证实的(下一步的真正门槛)**:
- **修复后首个受约束真跑(2026-08-10)**已完整通过 harvest→survey→ideate:6 篇 corpus、3 个 gap、
  2 个候选 idea;2/2 均在 rubric 以 3.0/5 淘汰,最终 0 个 accepted。全程 4 次 LLM 调用、
  20,141 token、约 ¥0.0431;survey 14,456 token,ideate(含两个逐 idea judge)5,685 token,
  未触发 100k/80k token 包络或重复工具收束。这证明链路、真实计量、引用前缀归一化与质量闸工作,
  **不证明模型能持续产出好 idea**;零通过比把平庸候选包装成 insight 更诚实。
- 人工 usefulness handoff 已替换为生产第四阶段 `evaluate`:默认两位独立、temperature=0 的 LLM
  按固定八轴 rubric 复评,二元结论分裂或任一轴相差 ≥1.5 时才请第三位;逐轴取中位数并把 judge 原始
  判词、用量、成本和机制修改建议一起持久化。首次旧产物复评为 4.22/5,两票一致“不值得继续”,
  其中 `idea_mechanism_depth=2.75`;两位均指出候选只是反应式指标/防御 wrapper。把生成契约改为
  `failure_cause → new_invariant → intervention_level` 后同题对照由 0/2 通过提升至 1/4,
  机理深度中位数升至 3.50,三位 judge 以 2:1 判定值得跟进。第二轮同时暴露空 method matrix 与
  语料外机制锚点,因此 survey 现要求 ≥80% 论文进入比较矩阵,ideate 要求每个 idea 用
  `corpus_anchor_id/corpus_delta` 锚定 linked gap 的库内证据。
- 同一次真跑曾有 2 篇 PDF 的页眉推断异常令整篇退化。根因不是没安装:三件套均为 1.27.2.3;
  实际缺陷是逐页调用每次重扫全篇页眉(O(N²))、单页异常拖垮整篇,以及 `lines` 表格策略把一页
  6,070 字符放大到 250,662 字符。现页眉只推断一次、异常只降级单页、策略改为 `lines_strict`,
  并加 32k/8× 页面膨胀熔断。两篇原失败样本复测均走完整 `pymupdf4llm`(77,332 / 184,442
  字符,17.82s / 135.20s),无 raw/partial 降级。
- **`IDEATE_GATE_THRESHOLD = 4.0` 是拍的,不是校准的**。需要一批真实跑出的 idea + 淘汰留档数据
  才能定它(设计已把它做成单个常量就是为了这一天)。
- **v2 修订的三档接地与 `low_confidence` 传导**已实现并有单测,但它本身是一次真跑暴露的问题的
  修补——下一次真跑很可能暴露下一个。**真跑是唯一能发现这类问题的手段。**

**所以 R5 之前应再做一批可复现的真实端到端基准跑**(固定方向/种子/模型,记录 token、成本、闸深度、
双 LLM judge usefulness verdict,有实质分歧时第三位裁决),拿真数据回答:闸的严格度对不对、成本多少、idea 能不能看。一次偶然成功也不足
以证明稳定;否则 R5/R6 仍是在未验证的地基上加盖。

---

## 7. 风险与诚实边界

| 风险 | 说明 | 缓解 |
|---|---|---|
| **idea 质量天花板** | LLM 造的 idea 可能全是平庸增量,再好的闸也只能淘汰缝合怪、造不出天才 idea | 定位为「资深博士生第一稿」;闸保证**下限**(不丢人),不承诺**上限**(拿奖);对抗式 reviewer 前置审稿视角 |
| **新颖性判断本身会错** | LLM-as-judge 可能漏掉一篇它没搜到的近邻工作,把旧 idea 判成新 | grounding 强制列 prior_art;判 novelty 时**强制联网搜近邻**(不靠记忆);仍标注「已尽力查重,非绝对保证」 |
| **穷尽调研不可能真穷尽** | web/arXiv 搜不全,总有漏 | 明示覆盖范围与检索时间;空白地图标注「基于已检索 N 篇」;支持用户补种子论文 |
| **成本** | 七阶段 + 多轮调研 + 逐篇解析 + 逐图子 agent = 单任务可能上百次 LLM 调用 | 阶段级 checkpoint(重跑不从头)+ parse-once 库(越用越省)+ idea/图数硬上限 + 镜数式 token 预算 |
| **图表数据造假** | 模型可能编数据画一张好看但假的图 | 数据图的数据必须可追溯到真实来源;闸拒绝无来源数据;区分「数据图」(严谨)与「示意图」(允许示意) |
| **语义检索缺失** | 库内目前只有 BM25,近邻方向复用时可能漏命中语义相近但用词不同的论文 | v1 先用 BM25 + arxiv_id 精确去重够用;若召回不足,v2 加轻量 embedding 索引(**不在本稿承诺**,单列决策) |
| **共享树并发** | 多 sibling 并行改同一 HEAD | 每期一 commit、精确 pathspec、失败先行 + NEUTER(照项目纪律) |
| **Overleaf 依赖外部服务** | 编译在 Overleaf 服务器,受其可用性/限流影响 | `compile_project` 失败有界重试 + 明确报错;LaTeX 工程本身可 `download_source` 落地,不锁死在云端 |

**最重要的一条诚实**:这个系统的价值不是「替代科研」,是**把一个方向从「零」推到「有据、查得尽、
自洽、可投稿格式」的第一稿,并让每一步都留下可核验的证据链**。它让人从「大海捞针的体力活」里解放出来,
专注在「这个 idea 到底对不对」的判断上 —— 判断权始终在人。

---

## 8. 待拍板问题(需要你定的几件事)

1. **产物定位**:目标是「投稿级完整论文雏形」(含方法+实验设计+图+LaTeX),还是先做到
   「方向报告 + 综述 + 评分过的创新点」(前三阶段就停,先验证智识核心)?
   —— 我倾向**先交付 R1–R3(库+综述+反A+B闸)**,这是价值密度最高、最能证明系统不是玩具的一段。
2. **实验执行**:阶段 5 只出「方案」,还是 v2 要接 hope/MLP 真跑小规模验证?(后者是大工程,单列)
3. **新颖性闸的严格度**:宁缺毋滥(高阈值,可能一个 idea 都不给)vs 多产但标注风险(低阈值)?
   —— 关系到用户体验,想听你的偏好。
4. **图表美学基线**:有没有偏好的期刊/会议图风格模板(配色、字体)可作为 matplotlib 样式表的起点?
5. ~~**入口形态**~~ —— **已定(R4 落地时拍板)**:一个总入口
   `produce_research(direction=, lang=, n_ideas=, seed_arxiv_ids=, harvest_n=)`,与 `produce_video`/
   `produce_report` 同构。**不拆成 `survey`/`ideate` 子工具**:两者共享同一个崩溃续跑 job 与
   同一份 `open_gaps.json` 契约,拆开会把阶段间的 checkpoint 语义推给调用方(模型),而模型不应负责
   编排阶段图。局部重跑的需求由底盘的**阶段级 checkpoint** 满足(同一 dedup key 重跑即续),
   不需要额外的入口。

---

## 9. 与既有设计稿的关系

- `docs/PRODUCTION_PIPELINE_DESIGN.md`:**本稿是它的第四个配方**,严格遵守三层模型;不改底盘,只用底盘。
  若 figures/typeset 暴露出底盘缺件(如二进制产物通道 `deliverable` 终于被第 N 个样本用上),
  按那份稿的 §9/§10 方法**用实测决定是否抽**,不提前造。
- `lib/paper/insight_engine`、`recommend_engine`、`citation_audit`、`terminology_audit`:
  **本稿的 ideate/survey/typeset 阶段是它们的直接延伸**,复用其接地、rubric、审计范式。
- `docs/PAPER_MEDIA_UX_DESIGN.md`:前端生产卡与阅读模式面板对齐时参照。
```
