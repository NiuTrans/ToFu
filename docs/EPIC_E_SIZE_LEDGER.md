# Epic E — Core Bundle 尺寸分类账（pt_3879f00e 记分牌）

> **本表是 Epic-E 的验收记分牌。** 此后每个 deferral slice 必须：①更新「分片流水」
> 一行（含**生产服务字节数实测**：curl 线上 index 广告的 core/feature/pack 三档）；
> ②触碰 top-20 判定表时同步改判定。Epic-E 的完成读作「core 压缩态降到目标线
> （当前暂定 **≤ 1.2 MB**）+ 每片有账」，不再是「三个具名文件」。

## 基线（2026-08-01 00:35，线上实测）

| 档位 | 文件 | 字节数 |
|---|---|---|
| core（压缩态） | `bundle-a3a3a443.js` | **1,550,424** |
| feature（压缩态） | `feature-8204ccdc.js` | 470,760 |
| i18n pack（单语） | `i18n-zh-fa9b8307.js` | 220,982（en 223,102） |
| core 源总账 | 144 文件 | **4,123 KB**（i18n.js 404KB 已拆出，不在服务 core 内） |

已完成降级：`core/cross_tab_sync.js` 53KB（sub-3A, `8aa9a1c6`）+
`core/health_stream_timer.js` 62KB（sub-3B, `6baf1083`），合计 115KB（core 源 ~2.8%）。
**打法教训：先拆的不是最大的——按「好拆」选 vs 按「大」选，本表把一切改为按账排。**

## `_BUNDLE_FILES` 尺寸 top-20 降级判定（2026-08-01 实测）

| # | 文件 | 字节 | 判定 | 一句话理由 |
|---|---|---|---|---|
| 1 | i18n.js | 404,150 | **已拆出** | pack 模式在服（sub-1）；源仍在清单内、构建期按 pack 替换 |
| 2 | ui/tool_rounds.js | 260,719→**206,246** | **拆分落地（sub-4, `fcddc420`）** | 首屏冷渲染子集留 core（实测定案）；conv-meta 富渲染族（40KB）+ Timer Watcher 块+ticker（18KB）拆出为 deferred `ui/tool_rounds_rich.js`（60KB）；派发 typeof 闸降级通用行 + 到达升级 pass |
| 3 | ui/chat_render.js | 136,701 | boot-critical | 消息渲染即首屏本体 |
| 4 | ui/sse_pipeline.js | 116,042 | boot-critical | 聊天流主管道 |
| 5 | api.js | 99,249 | boot-critical | 统一 API 客户端，`_CRITICAL_FILES` 成员 |
| 6 | tofu-scene.js | 95,830 | **已降级（sub-3C, `df664a2d`）** | 宠物场景，纯装饰；与 tofu-pet.js 同族合计 160KB 已出 core |
| 7 | ui/finish_info.js | 90,090 | 可降级（第三梯队） | 轮尾信息卡；首屏无可渲染轮次时不必需 |
| 8 | project.js→**project_state.js 23,958 + panel 67,114** | 89,198 | **拆分落地（sub-7）** | 状态子集留 core（boot/SSE/bar 全裸调用实测定案）+ 面板 67KB 降级 + 13 stub；反向裸调 typeof 闸 ×3 |
| 9 | core/conversations.js | 88,735 | boot-critical | 会话数据核心（sub-2 已 −41%，收尾中） |
| 10 | main/main_send_pipeline.js | 88,658 | boot-critical | 发送管道 |
| 11 | main.js | 74,539 | boot-critical | boot orchestrator，`_CRITICAL_FILES` 成员 |
| 12 | ui/conversation_list.js | 71,066 | boot-critical | 侧栏即首屏本体 |
| 13 | ui/streaming_ui.js | 65,522 | boot-critical | 流式渲染热路径 |
| 14 | tofu-pet.js | 64,763 | **已降级（sub-3C, `df664a2d`）** | 宠物本体；装饰 160KB 的另一半 |
| 15 | core/health_stream_timer.js | 61,573 | **已降级（sub-3B）** | 零 stub（无一次性接线）；闸 + idle prefetch |
| 16 | ui/streaming_render.js | 57,383 | boot-critical | 流式渲染热路径 |
| 17 | myday.js | 56,261 | **已降级（sub-6, `9b10125c`）** | My Day 面板；3 stub + 零外部调用方 + boot 块晚载安全 |
| 18 | ui/streaming_swarm_panel.js | 54,946 | **已降级（sub-5B）** | swarm 面板，非首屏；7 调用点装闸 + 通用行退化（同 sub-4 契约） |
| 19 | settings/providers/access_matrix.js | 54,558 | **已降级（sub-5A）** | 设置面板；3 调用点全部早已 typeof 闸——零改动可降级 |
| 20 | main/main_toolbar_ui.js | 54,496 | boot-critical | 顶栏即首屏本体 |

## tofu-scene + tofu-pet 普查（2026-08-01，sub-3C 工作单）

160KB 装饰族的 deferral 可行性实测结论（**近乎零改动可降级**）：

| 检查项 | 结果 |
|---|---|
| window 暴露 | 每模块恰好 1 个：`window.TofuScene` / `window.TofuPet`（IIFE 单命名空间） |
| 外部 JS 调用方 | **0**（grep 全仓，含 main.js / settings / preferences） |
| 唯一外部引用 | `index.html:787` `onclick="window.TofuPet&&window.TofuPet.cycleDecor()"` — **已天然 absence-safe**（`&&` 短路，模块缺席零 ReferenceError） |
| 自举方式 | 两个 IIFE 均 `DOMContentLoaded → _boot()`（已解析则立即 boot）——**无一次性 boot 接线可丢，无需 stub**（比照 health_stream_timer 判例） |
| 挂载目标 | `#projectBar`（index.html:779，默认 `display:none`，显隐由 project.js 驱动与宠物无关）；DOM 全部 _boot 时自建 |
| 布局位移风险 | **低**：bar 自身 `display:none` 起步 + `animation:fadeIn .3s`，晚到 ~2s 的宠物随 bar 淡入，无容器预留需求 |

**工作单：** 纯 manifest move（`_BUNDLE_FILES` → `_DEFERRED_FILES`），零闸零 stub；
配套 deferred 套件（manifest 双断言 + index.html onclick absence-safe 钉 + 无 stub 钉）
+ 农场物理验证（core 排除 TofuScene/TofuPet、feature 含）+ 本表流水行。

**已落地（sub-3C, `df664a2d`）：** 普查切片时复核全部成立；农场构建实测 core 排除
`window.TofuPet=`/`window.TofuScene=`、feature 含两者（8/8 PASS，prior 形态保持）；
套件 `test_frontend_tofu_pet_scene_deferred.py` 12 检查（failing-first 4 红 / NEUTER×2
精确：全回退 4 红、仅回退 pet 精确 2 红——逐文件鉴别力实证）。

## 分片流水（每片一行，含生产实测字节数）

| 日期 | 分片 | commit | 降级文件 | core 压缩态 | feature 压缩态 | 备注 |
|---|---|---|---|---|---|---|
| 2026-07-31 | sub-3A | `8aa9a1c6` | cross_tab_sync.js (53KB) | 1,532,269（2026-08-01 实测，`bundle-cc0b5197.js`） | 511,266（`feature-5f804ab4.js`） | stub + 3 闸；事故链见 JOURNAL 2026-08-01；生产数字为 sub-3A+3B 落地后实测 |
| 2026-08-01 | sub-3B | `6baf1083` | health_stream_timer.js (62KB) | 同上（与 3A 同批生效） | 同上 | 5 闸 + 零 stub 设计；农场同构构建 −13.9KB 净额（兄弟增量抵销部分） |
| 2026-08-01 | sub-3C | `df664a2d` | tofu-pet.js + tofu-scene.js (160KB) | **1,493,217**（`bundle-5c05d29b.js`，同日实测） | **549,924**（`feature-53a9cd44.js`） | 零闸零 stub（普查复核）；农场与生产**同 hash** 验证；runbook ALL GREEN（sub-3A+3B+3C 三片 14 项全过） |
| 2026-08-01 | sub-4 | `fcddc420` | tool_rounds.js 拆分（−58KB 源） | **1,460,290**（`bundle-827d3641.js`，同日实测） | **584,415**（`feature-f33bbae5.js`） | 拆分非 move：冷渲染留 core + conv-meta/timer-watcher 降级；行为 harness 双模态 + 升级后 wire-parity 闸 43 轮字节级（41 轮新旧逐字节一致）；农场与生产**同 hash**；runbook 20 项 ALL GREEN |
| 2026-08-01 | sub-5A | 见 HEAD | access_matrix.js (55KB) | **1,384,317**（`bundle-f53ca113.js`，同日实测，含兄弟增量 + 在飞 sub-6） | **666,260**（`feature-1cade5e9.js`） | **零新闸零 stub**（3 调用点全部早已 typeof 闸，`_stgMatrixOpen` 随模块走）；NEUTER×2 精确；农场 9/9；runbook 26 项 ALL GREEN |
| 2026-08-01 | sub-5B | 见 HEAD | streaming_swarm_panel.js (55KB) | 同上（与 5A 同批生效） | 同上 | 7 调用点装闸 + 通用行退化；注册套件重锚 deferred 不变量；NEUTER×2 精确；农场 10/10；e2e(visual+slow) 的 `_buildSwarmPanelHTML` 断言现依赖 idle prefetch 落地，已标记 |
| 2026-08-01 | sub-6 | `9b10125c` | myday.js + myday_tasks.js (65KB) | **1,384,317**（`bundle-f53ca113.js`，同日实测；与农场**同 hash**） | **666,260**（`feature-1cade5e9.js`） | 零闸 + 3 stub（openDailyReport/closeDailyReport/_mydayTriggerGenerate，py+js 双表）；零外部 JS 调用方（comm 实证）、`_myday` 态私有于双文件、boot 块 readyState 分支晚载安全；NEUTER×2 精确；农场 13/13；runbook 31 项 ALL GREEN |
| 2026-08-01 | sub-7 | 见 HEAD | project.js 拆分（state 24KB 留 core + panel 67KB 降级） | **1,351,102**（`bundle-c0d727ec.js`，同日实测） | **702,323**（`feature-c4756610.js`） | 第二个「状态留 core+面板降级」拆分；反向裸调 typeof 闸 ×3；13 stub；harness 双文件重指向 ×2（state-first + 响亮失败）；NEUTER×2 精确；环 106/106；runbook 34 项 ALL GREEN |
| 2026-08-01 | sub-8 | `48c1651f` | finish_info.js 拆分（cost-popover 族 24KB 降级为懒构建） | **1,273,849**（`bundle-4f0fc8ba.js`，同日实测，含兄弟增量） | **782,274**（`feature-2a61875f.js`） | 首个「懒构建」拆分：renderFinishInfo 不再内嵌预建 popover，改 stash `_costCtxByMsg`（var 非 const——跨 bundle 缝钉）+ 空占位，`_toggleCostPopover` stub 首击懒建；cache-break 短语族留 core（折叠栏 tooltip 冷渲）；modes harness 双模态行为证 + 自带 NC；影子树 NEUTER×2（1红/2红）；两 cache 套件重锚随批；runbook 39 项 ALL GREEN |
| 2026-08-01 | sub-9 | 见 HEAD | settings 面板族六件（update 48.7K / skills 21.5K / memory 16.9K / optimizer 13.9K / timer 13.6K / preferences 8.1K ≈ 123KB 整批 move） | **1,273,895**（`bundle-cab4b778.js`，同日实测；与 sub-8 行纠缠——见下） | **782,236**（`feature-756162a4.js`） | 首个「面板族整批 move」片：六件全用户触发零 boot 裸调；boot 接线三修（update.js `_onReady` 化——window-'load' 对晚到 bundle 永不再燃 / timer+optimizer 自举晚载安全 / mobile_panels 恒等再包 + `tofu:feature-bundle-loaded` land 事件重包 + pre-land kick）；gate+stub 组合（settings tab populate 三键改派发不跳票）；11 stub py+js 双表；套件 11 检查 failing-first 精确 5 红；NEUTER×2（回注 timer.js→out_of_core 红 / 摘 stub→loader 表红，cmp 字节还原）；环 77/77；runbook **42 项 ALL GREEN**；sub-7 位置锚 harness 随批重锚（memory.js→memory_skill_install.js） |
| 2026-08-01 | sub-9 收尾 | `05da24cd` | 同 sub-9 六件（+4 陷阱修，core +326B stub 条目） | **1,274,221**（`bundle-b9c7fb1f.js`，同日实测；农场/生产**同 hash**） | **782,236**（`feature-756162a4.js`） | 共享树联合批的 HEAD 自洽收尾（生产重建携树内修复过了 runbook，干净 HEAD 缺此批即破）：①**`_esc` 提级** core/escape_html.js——全仓唯一顶层 escapeHtml 别名，~62 core 调用点（artifacts 9/compaction-viewer 36/toast 3/log-clean 8/streaming_ui 2/translation_render 4）经 window 解析到 memory.js，随降级悬空；②**LoadGuard 摘 'toggleMemory'**——deferred 入口被 _notReady 预装则 `_installFeatureStub` typeof 跳过、懒 stub 永不安装（sub-6 openDailyReport 镜像坑，兄弟背书）；③main.js `_updateMemoryModalBtn` 裸调装闸（boot 工具态路径）；④+6 防御 stub（静态面板 onclick 全覆盖）；4 套件按落地形态校准（installer 留 core / IIFE readyState 分支 / pill-stub 即升温器）failing-first 22 红；NEUTER×2 精确（摘 stub→loader 表红 / 摘 _esc→提级红，cmp 字节还原）；环 **94/94**；runbook **42/42 ALL GREEN** |

| 2026-08-02 | sub-10 | `503859b9` | settings/ 整族 20 件（~403KB 源整批 move；branding.js 留 core——main.js:88/349 boot 裸调 `_modelShortName` 实证，非「no boot callers」） | **1,045,274**（`bundle-40dde573.js`，同日生产实测；与农场**同 hash**） | **1,012,457**（`feature-4b38a66c.js`） | **破线片——首个「整族降级」**：settings 族只渲染在用户触发的 Settings 模态内，core_panel 裸调链随件变 bundle 内调用；settings.js 头留 core（`_serverConfig` 被 main_input_handling 读）；4 入口 stub（openSettings/closeSettings/saveSettings/switchSettingsTab）+ LoadGuard 摘 'openSettings'（toggleMemory 同坑）；双套件 22 检查（两边界钉互相独立咬——branding NEUTER 双红）；环 96/96；runbook **52 项 ALL GREEN**。**core 1,045,274 < 1.2MB 验收线，生产达成，余量 ~155KB** |

## 目标线与当前差距

- **目标（暂定）**：core 压缩态 ≤ **1.2 MB**（待 owner 确认）。
- **当前（2026-08-02 生产实测）**：**1,045,274 B**（`bundle-40dde573.js`，十二片全部生效，runbook 52 项 ALL GREEN）⇒ **验收线已达成**：较 1.2MB（≈1,228,800 B）余量 **~183KB**，较 1,200,000 B 余量 **~155KB**（基线 1,550,424 → 累计 **−505KB 压缩态 / −32.6%**，含兄弟增量）。
- **累计已降级源码 ~1,123KB**（53+62+160+58+55+55+65+67+24+123+403）。feature bundle 1,012,457 B——**下一战场是 feature 自身体积**（已超 core 的 97%，懒加载族内部再分层：paper/orchestration/swarm 等冷中冷件），不属本 epic 验收口径。
- **Epic-E 验收判读（owner/claim-holder 终审）**：core ≤1.2MB ✅（生产实测 + runbook 52 项）+ 每片有账 ✅（12 行流水，片片含 commit/字节/NEUTER/runbook 证据链）。
