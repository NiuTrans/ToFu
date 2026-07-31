#!/usr/bin/env python3
"""tests/_migrate_charter_kinds.py — ONE-SHOT charter kind migration (2026-07-28).

Re-homes every legacy charter decision to the substrate its content belongs to
(owner-directed, epic pt_3023b980a4a2421f):

  keep    — binding rule → stays in the charter, backfilled with
            kind='invariant' + a one-line `summary` (the rule itself).
  memory  — methodology lesson → folded into ONE project memory
            (the guard-failure family is a single living document, not 7
            append-only entries).
  journal — completion/WONTFIX record → appended to JOURNAL.md (the audit
            trail), then removed from the charter.
  delete  — stale (superseded, or its content already lives in the north-star
            `content` column).
  rewrite — entry kept but its text narrowed to the binding part (the lesson
            half goes to the family memory).

ORDER IS LOAD-BEARING (owner): write-then-delete, verify before delete.
Lessons are verified searchable in the project memory corpus and reports are
verified present in JOURNAL.md BEFORE the charter row is rewritten.

Idempotent: each phase skips when its output marker already exists.
Dry-run by default; pass --apply to execute.

Usage:
    python tests/_migrate_charter_kinds.py            # dry-run: print the plan
    python tests/_migrate_charter_kinds.py --apply    # execute
    python tests/_migrate_charter_kinds.py --project /path/to/project
"""

from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, ROOT)

_JOURNAL = os.path.join(ROOT, 'JOURNAL.md')
_JOURNAL_MARKER = 'charter 决策大迁移(33 条 → kind 分流)'
_FAMILY_MEM_NAME = '测试守卫纪律家族（charter 路由迁入）'

# ── The curated classification table ─────────────────────────────────────────
# Matched by text PREFIX (robust to reordering); each prefix MUST match exactly
# one decision or the migration refuses to run. kind meanings above.

PLAN = [
    # ── keep: binding rules, backfill summary ──
    {'prefix': 'conv_ref 分页读取的行存储决策', 'action': 'keep',
     'summary': '取会话消息段 MUST 复用 lib/database/messages_rows.py::load_message_window,禁止自造 offset/range 查询;行存储读切换是数据迁移决策,必须先过 verify_conv_parity,不得夹带进任何渲染批次'},
    {'prefix': '行存储读切换的**完整性前置条件**', 'action': 'keep',
     'summary': '任何 conversation_messages 行存储读路径 MUST 先过 row_window_usable(行数≥blob 消息数)否则回落 blob——部分 backfill 静默丢结论是真正杀手,禁止只判空;任何异常朝 blob 失败关闭'},
    {'prefix': '后端错误透明传递担保', 'action': 'keep',
     'summary': '用户可见错误 MUST 走 typed error envelope;kind 必须在 KINDS 封闭枚举注册且 _TITLES/i18n/ERROR_KIND_LABELS 三处同步;_make_envelope 是唯一归一化咽喉;禁止新增绕过 envelope 的错误路径与未注册 kind'},
    {'prefix': 'LoopWatch STALLED 根因裁定', 'action': 'keep',
     'summary': 'LoopWatch STALLED 根因是 logging 写 FUSE 阻塞事件循环,不是 json.dumps(循环线程 0 次)——禁止按「dumps 挪到 to_thread」方案改动;PG 迁移不得当 stall 修复验收;定责以全线程转储为准,hotspot 榜不可用'},
    {'prefix': '请求检视器「状态轴」交互与三条不变量', 'action': 'keep',
     'summary': '请求检视器:状态行点击 MUST 内联打开状态面板(禁止死按钮);内联面板与抽屉详情 MUST 走 renderDebugBlocksInto 单渲染路径;状态负载 MUST 带 kind=state 拉取,禁止混 requests 轴'},
    {'prefix': 'Agent 能力复用铁律', 'action': 'keep',
     'summary': '新增 agent 驱动功能 MUST 建在共享底盘(agent_loop 的 run_agent_loop / production 的 ProductionRuntime);禁止新增私有多轮工具循环、私有中止、私有崩溃恢复;底盘缺能力时改底盘,不在调用方打补丁'},
    {'prefix': '事件通道调度契约', 'action': 'keep',
     'summary': '大脑派单事件为主、心跳为网:新增任何「创建即应启动」的工作项 MUST 走生产者侧事件缝(on_epic_posted/on_conv_idle/peer 发时排空),禁止新建等待心跳的通道;30s sweep 与 drain 恢复网不得删除或削弱'},
    {'prefix': '前端类型面「生成式声明」纪律', 'action': 'keep',
     'summary': '改 window.X 导出或增删全局符号后 MUST 重跑 gen_frontend_globals.py 并提交;禁止手写 declare var X 消音 tsc、禁止上调 typecheck BASELINE(=0);新增 scripts/ CI 脚本 MUST 同时加 .gitignore ! 例外与 export 白名单'},
    {'prefix': 'MCP 远程传输与凭证载体契约', 'action': 'keep',
     'summary': 'MCP 凭证脱敏是 FAIL-CLOSED 白名单,未分类字段一律丢弃,禁止改回按字段名排除;密钥单一真源=env,headers/url 只是 ${VAR} 模板,禁止新开第二个密钥存储;日志脱敏走 MCPConnectError._format 唯一咽喉,URL 按 query 参数粒度;transport 判定必须显式 is_stdio()'},
    {'prefix': '生活服务/出行厂商上架判据', 'action': 'keep',
     'summary': '厂商上架唯一判据:普通开发者能否自助拿到凭证——逐厂商实测,禁止行业级外推;落点按协议分(MCP→registry,Skill→catalog);「不建死卡片」禁令 MUST 与「自助可得的必须真在架上」补集守卫成对'},
    {'prefix': '携程/美团消费侧接入', 'action': 'keep',
     'summary': '不为携程商旅/美团走企业资质流程(判据是增量不足,非进不去);重开条件:(a)具体到店/外卖 agent 需求落地 或 (b)任一家向个人开发者发凭证——(b) MUST 重新实测,禁止凭印象或新闻稿'},
    {'prefix': '目录 UI「后端单一真源不得被前端手抄」', 'action': 'keep',
     'summary': '前端不得手抄后端枚举/清单,药丸集合 MUST 从 catalog 实际数据派生(未知项追加渲染而非丢弃);新增「会被 UI 展示」的 catalog 字段 MUST 同时有渲染点+结果守卫;凭证获取路径走 obtain_url/obtain_steps 结构化字段,禁止塞进 hint'},
    {'prefix': '子进程运行时环境的解析判据必须锚在「解释器属性」上', 'action': 'keep',
     'summary': '子进程运行时环境判据 MUST 锚在解释器属性(sys.prefix),禁止以 $CONDA_PREFIX 或 .tofu_env.json 为唯一判据;采纳目录必须 isfile 到目标库才收;export.py 会剥掉的东西不得成为功能必要条件——导出产物是第一等验收目标'},
    {'prefix': '安装加速的配置必须落在**执行路径的公共前缀**上', 'action': 'keep',
     'summary': '对两条安装后端都成立的配置(镜像/索引/缓存/代理)MUST 放在分叉前的公共前缀,禁止在分支内部配置全局加速参数;新增包管理器后端先实测它读哪个变量名(uv 不读 PIP_INDEX_URL);「必须活着到达用户」的文件 MUST 有导出存活守卫(export 与 git 两道门)'},

    # ── rewrite: entry kept but narrowed to its binding core ──
    {'prefix': '北极星目标补充', 'action': 'rewrite',
     'new_text': 'endpoint/orchestrator 迁移上共享 agent 底盘的顺序为 orchestrator 在前(2026-07-27,owner 拍板;依赖关系决定——endpoint 的 Worker turn 调用的 _run_single_turn 定义在 lib.tasks_pkg.orchestrator,先迁 endpoint 会把私有循环嵌套进底盘钩内,两层循环语义叠加),endpoint 等 orchestrator 上底盘后动工。北极星补句本体已并入 charter content 列。',
     'summary': 'endpoint/orchestrator 迁移顺序为 orchestrator 在前——_run_single_turn 定义在 orchestrator,先迁 endpoint 会把私有循环嵌套进底盘钩内形成两层循环;endpoint 等 orchestrator 上底盘后动工'},
    {'prefix': '事件类守卫必须校准「观测窗口」;共享 HEAD 上禁止以工作树为中间态', 'action': 'rewrite',
     'new_text': '共享 HEAD:禁止任何以工作树为中间态的操作,不止 A/B 验证(2026-07-28,epic pt_de6b74141e3141a4 收口;暂存分离同样禁止——git stash push --keep-index + checkout -- 曾同时抹掉兄弟的与自己的改动,且 stash pop 撞上兄弟保管 stash 留下 UU 冲突)。唯一正解:git diff > d.patch → 按 hunk 过滤 → git apply --cached——它只写 index、从不碰工作树。git add 之后 MUST 做 git diff --cached --name-only | wc -l 计数断言,数目不符立即 abort 并 git reset;已扫入的他人改动必须 --amend 摘出并原样放回工作树,不得代其提交。',
     'summary': '共享 HEAD 上禁止任何以工作树为中间态的操作(A/B 验证与暂存分离同禁 git stash);唯一正解 git diff > patch → 按 hunk 过滤 → git apply --cached(只写 index 不碰工作树);git add 后 MUST 计数断言,不符即 abort'},
    # ①②④⑤ lesson 部分迁往守卫家族记忆。

    # ── memory: methodology lessons → ONE guard-family memory ──
    {'prefix': '测试守卫必须断言「结果」而非「实现」', 'action': 'memory'},
    {'prefix': '守卫失效的第三种形态', 'action': 'memory'},
    {'prefix': '豁免/例外的依据必须是可执行断言', 'action': 'memory'},
    {'prefix': '扫描类守卫必须先验证「扫描面」', 'action': 'memory'},
    {'prefix': '守卫定位「文件」必须来自生产的单一真源', 'action': 'memory'},
    {'prefix': '前端正确性:真实浏览器测试必须监听浏览器自己的错误通道', 'action': 'memory'},
    # (#32 的 ①②④⑤ 课训段由 rewrite 项一并迁入,见 _GUARD_EXTRA_LESSON。)

    # ── journal: completion / WONTFIX records → JOURNAL.md ──
    {'prefix': 'Opus 5 适配收口结论(2026-07-26,实测后拍板 —— 本条**取代**', 'action': 'journal'},
    {'prefix': '回退链混缓存不兼容模型——实测否决', 'action': 'journal'},
    {'prefix': 'breakpoint-lost / extended-ttl beta flap', 'action': 'journal'},
    {'prefix': 'key 轮换粘性', 'action': 'journal'},
    {'prefix': 'opus-5 evaDaily 体缓存几乎不命中', 'action': 'journal'},
    {'prefix': '[perf] LoopWatch 非阻塞日志修复已实证生效', 'action': 'journal'},
    {'prefix': 'TTFT 首字节看门狗 + 等待心跳已落地', 'action': 'journal'},
    {'prefix': '[perf] audit_log 残余同步写已修复', 'action': 'journal'},
    {'prefix': '行存储写路径迁移全期收口', 'action': 'journal'},

    # ── delete: stale (superseded / already merged into content) ──
    {'prefix': 'Opus 5 适配收口结论(2026-07-26,实测后拍板):**`mid-conversation', 'action': 'delete',
     'note': 'superseded by the newer same-named entry (which goes to JOURNAL)'},
    {'prefix': '建议在北极星目标「tofu项目需要具有长期扩展性', 'action': 'delete',
     'note': 'proposal text; the sentence now lives in the content column'},
]

# #32 的课训部分(①观测窗口 ②NEUTER 有效性 ④覆盖入口 ⑤onclick 守卫),随 rewrite
# 迁入守卫家族记忆;charter 只留 ③ 共享 HEAD 规则。
_GUARD_EXTRA_LESSON = (
    '事件类守卫必须校准「观测窗口」(#32 课训段迁入):扫描类守卫问「扫到了哪些东西」,'
    '事件/时序类守卫必须问「我的观测窗口覆盖了被测行为真正发生的时刻吗」——'
    'settings openSettings() 一趟调完全部 _populate*Tab,switchSettingsTab 之后只翻 '
    'CSS class,在 open 之后才清错误缓冲等于把唯一能看见渲染崩溃的窗口自己丢掉。'
    'NEUTER 注入本身必须先验证有效性:改完 MUST node --check(JS)/ ast.parse(Py) '
    '确认语法合法且改动真的进入运行路径——破坏压缩器的注入与「守卫没咬」在输出上'
    '完全一样。面板级浏览器覆盖入口必须实测得出,票面猜的函数名一个都不存在。')


def _load_charter(project_path):
    from lib.conversations.project_charter import read_charter
    rec = read_charter(project_path)
    if not rec.get('exists'):
        raise SystemExit(f'no charter for {project_path}')
    return rec


def _match_plan(decisions):
    """Resolve every PLAN entry to exactly one decision index, or refuse."""
    used = set()
    resolved = []
    for entry in PLAN:
        hits = [i for i, d in enumerate(decisions)
                if (d.get('text') or '').startswith(entry['prefix'])]
        if len(hits) != 1:
            raise SystemExit(
                f"PLAN prefix {entry['prefix'][:40]!r} matched {len(hits)} "
                f"decisions (need exactly 1) — refusing to run")
        if hits[0] in used:
            raise SystemExit(f"prefix {entry['prefix'][:40]!r} double-matched")
        used.add(hits[0])
        resolved.append((hits[0], entry))
    uncovered = [i for i in range(len(decisions)) if i not in used]
    return resolved, uncovered


def _append_journal_section(report_texts):
    """Prepend the migration section right after JOURNAL.md's header comment."""
    with open(_JOURNAL, encoding='utf-8') as f:
        src = f.read()
    if _JOURNAL_MARKER in src:
        print('  [journal] marker present — skip (idempotent)')
        return
    today = time.strftime('%Y-%m-%d')
    parts = [f'\n### {today} — charter 决策大迁移(33 条 → kind 分流):9 条完工/否决记录自 charter 迁入本志。\n',
             '> 背景:charter 只留约束未来决策的 invariant;以下为收口/实测否决的审计留痕,逐条原文迁移,未改写。\n']
    for t in report_texts:
        first = t.split('\n', 1)[0].strip()
        parts.append(f'\n#### {first[:80]}\n\n{t}\n')
    section = ''.join(parts)
    # Insert after the leading <!-- ... --> header comment if present, else top.
    if src.startswith('<!--'):
        end = src.index('-->') + 3
        new = src[:end] + '\n' + section + src[end:]
    else:
        new = section + '\n' + src
    tmp = _JOURNAL + '.migrate-tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(new)
    os.replace(tmp, _JOURNAL)
    if _JOURNAL_MARKER not in open(_JOURNAL, encoding='utf-8').read():
        raise SystemExit('journal write verification FAILED')
    print(f'  [journal] appended {len(report_texts)} report(s)')


def _write_family_memory(project_path, lesson_texts):
    from lib.memory.relevance._search import search_memories_scored
    from lib.memory.storage import create_memory, list_memories, update_memory
    existing = [m for m in list_memories(project_path, scope='project')
                if m.get('name') == _FAMILY_MEM_NAME]
    today = time.strftime('%Y-%m-%d')
    body_parts = ['charter 决策大迁移(2026-07-28)迁入:以下 7 段原以 charter 决策形式'
                  '逐条 append,实为同一家族的方法论教训——守卫必须断言结果、扫描面、'
                  '观测窗口、NEUTER 反验、符号单一真源、浏览器错误通道。新变种 MUST '
                  '并入本文件(update_memory),禁止再开第 N 个独立条目。\n']
    for t in lesson_texts:
        body_parts.append(f'\n---\n\n### 变体（{today} 迁移）\n\n{t}\n')
    body_parts.append(f'\n---\n\n### 变体（{today} 迁移）\n\n{_GUARD_EXTRA_LESSON}\n')
    body = ''.join(body_parts)
    if existing:
        mem = existing[0]
        if lesson_texts[0][:80] in (mem.get('body') or ''):
            print('  [memory] family memory already migrated — skip (idempotent)')
            return mem['id']
        update_memory(mem['id'], {'body': (mem.get('body') or '').rstrip()
                                  + '\n' + body}, project_path=project_path)
        mem_id = mem['id']
    else:
        mem = create_memory(
            name=_FAMILY_MEM_NAME,
            description='守卫必须断言结果而非实现;扫描面、观测窗口先验证再断言;NEUTER 必打边缘形态并反验;生产符号单一真源;真实浏览器错误通道分级。新变种并入本文件,禁止开第 N 个独立条目。',
            body=body, tags=['charter-lesson', 'testing', 'guards'],
            scope='project', project_path=project_path)
        mem_id = mem['id']
    # Verify searchable (owner's write-then-verify order).
    hits = search_memories_scored('守卫 扫描面 观测窗口 NEUTER', project_path,
                                  top_k=5, scope='project')
    if not any(m['id'] == mem_id for _, m in hits):
        raise SystemExit('memory search verification FAILED — refusing to '
                         'delete the charter entries')
    print(f'  [memory] family memory {mem_id[:40]} written + searchable '
          f'({len(lesson_texts)} variants)')
    return mem_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--project', default=ROOT)
    args = ap.parse_args()
    project_path = os.path.abspath(args.project)

    rec = _load_charter(project_path)
    decisions = rec['decisions']
    resolved, uncovered = _match_plan(decisions)

    if uncovered:
        print('UNCOVERED decisions (add them to PLAN first):')
        for i in uncovered:
            print(f"  [{i}] {(decisions[i].get('text') or '')[:70]}")
        raise SystemExit(1)

    by_action = {}
    for i, e in resolved:
        by_action.setdefault(e['action'], []).append(i)
    print(f'charter {project_path}: {len(decisions)} decisions')
    for act in ('keep', 'rewrite', 'memory', 'journal', 'delete'):
        print(f'  {act:8s}: {len(by_action.get(act, []))}')

    if not args.apply:
        print('\nDRY-RUN plan (per decision):')
        for i, e in resolved:
            head = (decisions[i].get('text') or '').split('\n', 1)[0][:60]
            print(f"  [{i:2d}] {e['action']:7s} {head}")
            if e.get('summary'):
                print(f"       summary: {e['summary'][:100]}")
        print('\nRe-run with --apply to execute.')
        return

    # Phase A: lessons → ONE family memory (verified searchable).
    lesson_texts = [(decisions[i].get('text') or '')
                    for i, e in resolved if e['action'] == 'memory']
    # #32 的课训段随 rewrite 一并迁入。
    _write_family_memory(project_path, lesson_texts)

    # Phase B: reports → JOURNAL.md (verified present).
    report_texts = [(decisions[i].get('text') or '')
                    for i, e in resolved if e['action'] == 'journal']
    _append_journal_section(report_texts)

    # Phase C: rebuild the decisions list (single version bump).
    from lib.conversations.project_charter import _persist_charter
    from lib.database import DOMAIN_CHAT, get_thread_db
    keep_map = {i: e for i, e in resolved if e['action'] in ('keep', 'rewrite')}
    new_decisions = []
    for i, d in enumerate(decisions):
        if i not in keep_map:
            continue
        e = keep_map[i]
        d = dict(d)
        if e['action'] == 'rewrite':
            d['text'] = e['new_text']
        d['kind'] = 'invariant'
        d['summary'] = e['summary']
        new_decisions.append(d)
    db = get_thread_db(DOMAIN_CHAT)
    _persist_charter(db, project_path, rec['content'], new_decisions,
                     'charter-kind-migration', rec['version'] + 1)
    db.commit()
    try:
        from lib.conversations.project_feed import emit_project_event
        emit_project_event(
            project_path, 'charter-kind-migration', 'decided',
            f'charter kind 分流迁移:{len(decisions)} 条 → '
            f'{len(new_decisions)} 条 invariant(带 summary);'
            f'{len(lesson_texts)} 条教训迁入守卫家族记忆,'
            f'{len(report_texts)} 条报告迁入 JOURNAL',
            payload={'version': rec['version'] + 1,
                     'charterKindMigration': True})
    except Exception as e:
        print(f'  [feed] decided event skipped (persisted): {e}')

    # Phase D: verify the final state.
    after = _load_charter(project_path)
    assert len(after['decisions']) == len(keep_map), (
        f"expected {len(keep_map)} decisions, got {len(after['decisions'])}")
    for d in after['decisions']:
        assert d.get('kind') == 'invariant' and (d.get('summary') or '').strip(), \
            f"entry missing kind/summary: {(d.get('text') or '')[:50]}"

    from lib.conversations.project_charter import (
        render_charter_injection_block)
    block = render_charter_injection_block(project_path)
    print(f'\nDONE. decisions {len(decisions)} → {len(after["decisions"])}; '
          f'injection block now {len(block)} chars '
          f'(version {after["version"]})')


if __name__ == '__main__':
    main()
