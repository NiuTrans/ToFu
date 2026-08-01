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
| 2 | ui/tool_rounds.js | 260,719 | **boot-critical（实测定案 2026-08-01）** | 首屏恢复含工具轮的会话走 `chat_render.js:1499 → renderToolRoundsHTML`（裸调用，非闸）——整体 move 会让工具气泡首屏空白；只能走「冷渲染子集留 core + 交互增强（审批钮/计时器/QR/inspect）降级」的拆分，工作量大于普通 deferral，排期在装饰族之后 |
| 3 | ui/chat_render.js | 136,701 | boot-critical | 消息渲染即首屏本体 |
| 4 | ui/sse_pipeline.js | 116,042 | boot-critical | 聊天流主管道 |
| 5 | api.js | 99,249 | boot-critical | 统一 API 客户端，`_CRITICAL_FILES` 成员 |
| 6 | tofu-scene.js | 95,830 | **可降级（候选 #2）** | 宠物场景，纯装饰；与 tofu-pet.js 同族合计 160KB，首屏零必要 |
| 7 | ui/finish_info.js | 90,090 | 可降级（第三梯队） | 轮尾信息卡；首屏无可渲染轮次时不必需 |
| 8 | project.js | 89,198 | 可降级（第三梯队） | 项目/设置面板，owner 指定第三梯队 |
| 9 | core/conversations.js | 88,735 | boot-critical | 会话数据核心（sub-2 已 −41%，收尾中） |
| 10 | main/main_send_pipeline.js | 88,658 | boot-critical | 发送管道 |
| 11 | main.js | 74,539 | boot-critical | boot orchestrator，`_CRITICAL_FILES` 成员 |
| 12 | ui/conversation_list.js | 71,066 | boot-critical | 侧栏即首屏本体 |
| 13 | ui/streaming_ui.js | 65,522 | boot-critical | 流式渲染热路径 |
| 14 | tofu-pet.js | 64,763 | **可降级（与 #6 同族）** | 宠物本体；装饰 160KB 的另一半 |
| 15 | core/health_stream_timer.js | 61,573 | **已降级（sub-3B）** | 零 stub（无一次性接线）；闸 + idle prefetch |
| 16 | ui/streaming_render.js | 57,383 | boot-critical | 流式渲染热路径 |
| 17 | myday.js | 56,261 | 可降级（第三梯队） | My Day 面板，用户动作触发 |
| 18 | ui/streaming_swarm_panel.js | 54,946 | 可降级（第三梯队） | swarm 面板，非首屏 |
| 19 | settings/providers/access_matrix.js | 54,558 | 可降级（第三梯队） | 设置面板；settings 族应整族评估 |
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

## 分片流水（每片一行，含生产实测字节数）
## 分片流水（每片一行，含生产实测字节数）

| 日期 | 分片 | commit | 降级文件 | core 压缩态 | feature 压缩态 | 备注 |
|---|---|---|---|---|---|---|
| 2026-07-31 | sub-3A | `8aa9a1c6` | cross_tab_sync.js (53KB) | 1,550,424 | 470,760 | stub + 3 闸；事故链见 JOURNAL 2026-08-01 |
| 2026-08-01 | sub-3B | `6baf1083` | health_stream_timer.js (62KB) | 生产重启前不变（冻结清单） | 同左 | 5 闸 + 零 stub 设计；农场同构构建 2,289,664→2,275,757（−13.9KB 净额，同期兄弟新增 stall_watch 等抵销部分）；生产字节数随重启更新 |

## 目标线与当前差距

- **目标（暂定）**：core 压缩态 ≤ **1.2 MB**（待 owner 确认）。
- **当前**：1,550,424 B ⇒ 差距 ~350 KB。
- **已排队**：tofu-scene+tofu-pet 160KB（下一片）+ tool_rounds 拆分（冷渲染子集留 core 后可释放大头，需先设计拆分缝）⇒ 目标可达但路径在拆分不在整体 move。
