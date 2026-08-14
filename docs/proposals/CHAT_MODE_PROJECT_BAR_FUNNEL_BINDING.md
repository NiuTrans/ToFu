# 票据(未排期):Chat 档位 ⟺ 项目栏的漏斗级强制绑定(方案 B)

状态:**独立票据,未实施**。A+C 修复(2026-08-14,见 JOURNAL)已覆盖当前全部已知脱钩路径;
本提案是结构性根治的候选形态,采纳前需先解决下述持久化时序风险。

## 动机

档位旋钮(chatMode)与项目栏(projectState.active)是同一状态的两个投影,当前一致性靠
**每个调用点自觉**维持:`setChatMode`/`onProjectAttached`/`onProjectCleared`/
`_restoreConvToolState` 的双向钳制、`_restoreConvProject` 的守卫+降级。任何一个新调用点
忘记同步,脱钩就会复发。方案 B 把不变量收到唯一漏斗 `_updateProjectUI`(所有
projectState 变化都经过它):无栏必降档、有栏必升档,一处强制、全局生效。

## 为什么现在不做

1. **临时清空窗口的持久化毒**:`_restoreConvProject` 在 `await Api.project.setPaths` 之前
   会先 `_clearProjectStateLocal()` 把栏清掉(跨机房下这个窗口可达秒级)。漏斗级降级若在
   该窗口触发,会把全局 `chatMode` 置 `'chat'`;此时用户任意一次 toggle 触发的
   `_saveConvToolState` 会把 `'chat'` **持久化进一个带 projectPath 的对话** —— 正是恢复
   钳制 (b) 要 heal 的镜像毒。A+C 用"逐调用守卫、先守卫后降级"把降级时机钉在响应落地
   之后,漏斗方案则要把这套时序管控推广到**每一个**状态变化处,复杂度陡增。
2. **project SSE 事件也过漏斗**:后台任务的项目事件经 `_applyProjectData` 触发
   `_updateProjectUI`,此时"当前激活对话"与"事件所属对话"可能不同,漏斗升降级需要额外
   的归属判定,否则又是跨对话污染的一个新入口。
3. **paint 与 persist 的边界未定**:当前持久化语义由 `onProjectAttached`/`onProjectCleared`
   承担(带 `_saveConvToolState`);漏斗强制若做 paint-only,需要一个独立的"何时把档位
   写回对话存储"的契约,否则要么丢持久化、要么过量持久化。

## 采纳前提

- 引入 projectState 变更序号(单调 seq)或"恢复中"标志,让漏斗区分**终态清空**与
  **临时清空**(A+C 的守卫已示范了 seq 思路可复用);
- 明确 SSE 事件归属判定;
- `tests/test_chat_mode_project_bar_binding.py` 全绿作为前置棘轮,新增漏斗级用例
  (临时清空窗口内 toggle 不得持久化降级)。

## 替代(当前生效)

维持 A+C:切换路径无条件恢复工具态 + `_restoreConvProject` 逐调用守卫 + 失败分支经
`onProjectCleared` 降档持久化,由 `tests/test_chat_mode_project_bar_binding.py`
(16 针,含 5 个 poisoned-NC)钉住。
