#!/usr/bin/env python3
"""
bench_swarm_vs_single.py — Swarm vs Single-Agent 对比测试
═══════════════════════════════════════════════════════════

场景: "探索本项目并生成安全审计 / 代码质量报告"
  - 这些任务天然适合分解为多个并行子任务
  - Swarm 模式可以为每个维度分配独立 agent 并行执行
  - Single 模式则需要顺序完成所有分析

指标:
  ① 耗时 (wall-clock seconds)
  ② 产出长度 & 结构完整度 (自动解析)
  ③ 质量评分 (LLM-as-Judge, 5 维度打分)

用法:
  python debug/bench_swarm_vs_single.py [--server URL] [--model MODEL]
  python debug/bench_swarm_vs_single.py --scenario 0        # 只跑安全审计
  python debug/bench_swarm_vs_single.py --no-judge           # 跳过 LLM 评审
"""

import argparse, json, time, sys, os, re, textwrap, hashlib
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests

# ─── 配置 ─────────────────────────────────

DEFAULT_SERVER = "http://localhost:15000"
DEFAULT_MODEL  = ""   # 空 = 使用服务器默认模型

# 每个场景的 timeout 上限 (秒)
TASK_TIMEOUT = 900   # 15 分钟

# Poll 间隔 (秒)
POLL_INTERVAL = 3

# ─── 场景定义 ──────────────────────────────

SCENARIOS = [
    {
        "name": "🔒 安全审计 (Security Audit)",
        "description": "多维度项目安全分析 — 天然适合并行分解",
        "needs_project": True,
        "prompt": textwrap.dedent("""\
            请对当前项目进行全面的安全审计，分析以下 4 个维度并生成一份结构化报告：

            1. **认证与授权** — 检查 API 端点是否有适当的认证保护、是否存在未授权访问风险
            2. **注入攻击面** — 检查 SQL 注入、命令注入、XSS 等输入验证问题
            3. **信息泄露** — 检查日志中是否打印敏感信息、错误响应是否暴露内部细节
            4. **DoS / 资源耗尽** — 检查是否有适当的速率限制、文件上传大小限制、超时处理

            要求：
            - 每个维度至少列出 3 个具体发现 (标明文件名和行号)
            - 对每个发现标注风险等级 (高/中/低)
            - 给出具体的修复建议和示例代码
            - 最后生成一个汇总评分 (每个维度 1-10 分)
        """),
    },
    {
        "name": "📊 代码质量分析 (Code Quality)",
        "description": "多文件代码质量评审 — 并行检查不同模块",
        "needs_project": True,
        "prompt": textwrap.dedent("""\
            请对当前项目进行代码质量分析，覆盖以下 4 个方面：

            1. **错误处理** — 检查是否有裸 except、静默吞错、缺少 logging 的情况
            2. **并发安全** — 检查共享状态是否有锁保护、线程安全问题
            3. **API 设计** — 检查路由命名一致性、参数验证、响应格式统一性
            4. **性能隐患** — 检查 N+1 查询、不必要的全表扫描、大对象内存驻留

            要求：
            - 每个方面至少列出 3 个具体发现 (标明文件名和行号)
            - 对每个发现标注严重程度 (严重/警告/建议)
            - 给出具体的修复代码示例
            - 最后给出整体代码健康度评分 (1-100)
        """),
    },
]


# ─── 数据结构 ──────────────────────────────

@dataclass
class RunResult:
    """单次运行的结果"""
    mode: str             # "single" | "swarm"
    scenario: str         # 场景名
    elapsed: float = 0.0  # 耗时 (秒)
    output: str = ""      # LLM 最终输出
    thinking: str = ""    # thinking 输出
    error: Optional[str] = None  # 错误信息
    tool_rounds: int = 0         # 工具调用轮数
    agent_count: int = 0         # swarm agent 数量
    events_summary: list = field(default_factory=list)  # 关键事件摘要
    # 自动计算字段
    char_count: int = 0
    word_count: int = 0
    section_count: int = 0       # markdown 章节数
    code_block_count: int = 0    # 代码块数

    def __post_init__(self):
        self._compute_stats()

    def _compute_stats(self):
        if self.output:
            self.char_count = len(self.output)
            self.word_count = len(self.output.split())
            self.section_count = len(re.findall(r'^#{1,4}\s', self.output, re.MULTILINE))
            self.code_block_count = len(re.findall(r'```', self.output)) // 2


@dataclass
class JudgeScore:
    """LLM 评分结果"""
    completeness: int = 0     # 覆盖全面性 (1-10)
    depth: int = 0            # 分析深度 (1-10)
    actionability: int = 0    # 可操作性 (1-10)
    accuracy: int = 0         # 引用准确性 (1-10)
    structure: int = 0        # 结构清晰度 (1-10)
    total: float = 0.0        # 加权总分
    rationale: str = ""       # 评分理由
    winner: str = ""          # "single" | "swarm" | "tie"


# ─── 核心逻辑 ─────────────────────────────────────────────

class SwarmBenchmark:
    """驱动对比测试的核心类"""

    def __init__(self, server_url: str, model: str = "", project_path: str = ""):
        self.server = server_url.rstrip("/")
        self.model = model
        self.project_path = project_path
        self.results = []

    def check_server(self):
        """检查服务器是否在线"""
        try:
            r = requests.get(f"{self.server}/api/health", timeout=5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        # fallback: try root
        try:
            r = requests.get(self.server, timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    # ────────────────────────────────────────
    #  提交任务
    # ────────────────────────────────────────

    def submit_task(self, prompt: str, swarm_enabled: bool,
                    needs_project: bool = True) -> str:
        """POST /api/chat/start 提交任务, 返回 task_id.

        服务端期望的格式 (routes/chat.py → chat_start):
            {
                "convId": "...",
                "messages": [{"role": "user", "content": "..."}],
                "config": {
                    "model": "...",
                    "swarmEnabled": true/false,
                    "projectPath": "/...",
                    "preset": "opus",
                    "thinkingEnabled": true,
                    ...
                }
            }
        """

        conv_id = f"bench_{hashlib.md5(prompt.encode()).hexdigest()[:8]}_{int(time.time())}"

        config = {
            "swarmEnabled": swarm_enabled,
            "thinkingEnabled": True,
            "preset": "opus",
            "thinkingDepth": "medium",
            "searchMode": "off",
            "fetchEnabled": False,
            "skillsEnabled": False,
        }
        if self.model:
            config["model"] = self.model

        # 如果场景需要项目, 设置 projectPath
        if needs_project and self.project_path:
            config["projectPath"] = self.project_path

        payload = {
            "convId": conv_id,
            "messages": [{"role": "user", "content": prompt}],
            "config": config,
        }

        r = requests.post(
            f"{self.server}/api/chat/start",
            json=payload,
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  ❌ 服务端返回 {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
        data = r.json()
        return data.get("taskId") or data.get("task_id") or data.get("id")

    # ────────────────────────────────────────
    #  通过 SSE + poll 混合模式等待任务完成
    # ────────────────────────────────────────

    def wait_for_task(self, task_id: str, mode: str,
                      scenario_name: str) -> RunResult:
        """
        主策略: 通过 SSE 读取实时事件, 如果 SSE 断开则 fallback 到 poll.
        无论哪条路径, 最终都通过一次 poll 拿到完整的 content/thinking.
        """

        output = ""
        thinking = ""
        error = None
        events_summary = []
        tool_rounds = 0
        agent_count = 0
        spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        spin_i = 0
        t0 = time.time()
        got_done = False

        # ── Phase 1: SSE streaming ──
        try:
            with requests.get(
                f"{self.server}/api/chat/stream/{task_id}",
                stream=True, timeout=(10, TASK_TIMEOUT)
            ) as resp:
                resp.raise_for_status()

                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue

                    try:
                        evt = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    evt_type = evt.get("type", "")
                    elapsed = time.time() - t0

                    # ── state 快照 (SSE 首个事件) ──
                    if evt_type == "state":
                        output = evt.get("content", "")
                        thinking = evt.get("thinking", "")
                        status = evt.get("status", "running")
                        if status in ("done", "error"):
                            got_done = True
                            if status == "error":
                                error = evt.get("error", "Unknown error")
                            break

                    # ── 增量内容 ──
                    elif evt_type == "delta":
                        if "content" in evt:
                            output += evt["content"]
                        if "thinking" in evt:
                            thinking += evt["thinking"]

                    # ── 阶段信息 ──
                    elif evt_type == "phase":
                        phase = evt.get("phase", "")
                        detail = evt.get("detail", "")
                        rnd = evt.get("round", 0)
                        if rnd > tool_rounds:
                            tool_rounds = rnd
                        events_summary.append(f"[{elapsed:.0f}s] phase: {phase} — {detail}")

                    # ── 工具调用 ──
                    elif evt_type == "tool_start":
                        rn = evt.get("roundNum", 0)
                        query = evt.get("query", "")[:60]
                        tool_name = evt.get("toolName", "?")
                        if rn > tool_rounds:
                            tool_rounds = rn
                        events_summary.append(f"[{elapsed:.0f}s] tool: {tool_name} — {query}")

                    elif evt_type == "tool_result":
                        pass  # 工具返回结果, 已在 tool_start 里记

                    # ── Swarm 事件 ──
                    elif evt_type == "swarm_plan":
                        agents = evt.get("agents", [])
                        agent_count = max(agent_count, len(agents))
                        events_summary.append(f"[{elapsed:.0f}s] swarm plan: {len(agents)} agents")

                    elif evt_type == "swarm_progress":
                        done_n = evt.get("done", 0)
                        total_n = evt.get("total", 0)
                        events_summary.append(f"[{elapsed:.0f}s] swarm progress: {done_n}/{total_n}")

                    # ── 完成 ──
                    elif evt_type == "done":
                        got_done = True
                        if evt.get("error"):
                            error = evt["error"]
                        break

                    # ── 终端旋转进度条 ──
                    spin_i = (spin_i + 1) % len(spinner_chars)
                    status_str = f"output={len(output)} chars"
                    if tool_rounds:
                        status_str += f", rounds={tool_rounds}"
                    if agent_count:
                        status_str += f", agents={agent_count}"
                    sys.stdout.write(f"\r  {spinner_chars[spin_i]} [{elapsed:6.1f}s] {status_str}          ")
                    sys.stdout.flush()

        except requests.exceptions.Timeout:
            elapsed = time.time() - t0
            print(f"\n  ⏰ SSE 超时 ({elapsed:.0f}s)")
        except requests.exceptions.ConnectionError:
            elapsed = time.time() - t0
            print(f"\n  ⚡ SSE 连接断开 ({elapsed:.0f}s)")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"\n  ❌ SSE 异常: {e}")

        # ── Phase 2: Poll 兜底 (确保拿到完整结果) ──
        # 无论 SSE 是否收到 done, 都通过 poll 拿一次最终结果
        # 因为 SSE delta 可能丢失或不完整, poll 返回的是完整 content
        if not got_done:
            print(f"\n  ⚡ SSE 未收到 done, 切换 poll 模式...")

        final_output, final_thinking, final_error, final_status = self._poll_until_done(
            task_id, t0, spinner_chars
        )

        # poll 拿到的是完整内容, 用它覆盖 SSE 的增量拼接
        if final_output:
            output = final_output
        if final_thinking:
            thinking = final_thinking
        if final_error:
            error = final_error

        elapsed = time.time() - t0
        print(f"\r  ✅ [{elapsed:6.1f}s] 完成! output={len(output)} chars"
              f"{', rounds=' + str(tool_rounds) if tool_rounds else ''}"
              f"{', agents=' + str(agent_count) if agent_count else ''}          ")

        result = RunResult(
            mode=mode, scenario=scenario_name,
            elapsed=elapsed, output=output, thinking=thinking,
            error=error, tool_rounds=tool_rounds, agent_count=agent_count,
            events_summary=events_summary,
        )
        result._compute_stats()
        return result

    def _poll_until_done(self, task_id: str, t0: float,
                         spinner_chars: str) -> tuple:
        """
        轮询 /api/chat/poll/<task_id> 直到任务完成或超时.
        返回 (output, thinking, error, status)
        """
        spin_i = 0
        while True:
            elapsed = time.time() - t0
            if elapsed > TASK_TIMEOUT:
                return ("", "", f"Poll timeout ({TASK_TIMEOUT}s)", "timeout")

            try:
                r = requests.get(
                    f"{self.server}/api/chat/poll/{task_id}",
                    timeout=10,
                )
                if r.status_code == 404:
                    # 任务可能已清理
                    return ("", "", "Task not found (404)", "error")
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                time.sleep(POLL_INTERVAL)
                continue

            status = data.get("status", "unknown")
            content = data.get("content", "")
            thinking = data.get("thinking", "")
            err = data.get("error")

            spin_i = (spin_i + 1) % len(spinner_chars)
            sys.stdout.write(
                f"\r  {spinner_chars[spin_i]} [{elapsed:6.1f}s] "
                f"poll: status={status}, output={len(content)} chars          "
            )
            sys.stdout.flush()

            if status in ("done", "error"):
                return (content, thinking, err, status)

            time.sleep(POLL_INTERVAL)

    # ────────────────────────────────────────
    #  完整流程: 提交 + 等待
    # ────────────────────────────────────────

    def run_task(self, prompt: str, swarm_enabled: bool,
                 scenario_name: str, needs_project: bool = True) -> RunResult:
        """提交一个任务并等待完成"""

        mode = "swarm" if swarm_enabled else "single"
        print(f"\n  🚀 提交任务 [{mode.upper()}] ...")

        try:
            task_id = self.submit_task(prompt, swarm_enabled, needs_project)
            print(f"  📋 Task ID: {task_id}")
        except Exception as e:
            print(f"  ❌ 提交失败: {e}")
            return RunResult(mode=mode, scenario=scenario_name, error=str(e))

        return self.wait_for_task(task_id, mode, scenario_name)

    # ────────────────────────────────────────
    #  LLM-as-Judge 评审
    # ────────────────────────────────────────

    def judge(self, result_single: RunResult, result_swarm: RunResult,
              scenario_name: str) -> Optional[JudgeScore]:
        """用 LLM 对两份报告进行盲评"""

        if not result_single.output or not result_swarm.output:
            print("  ⚠️ 缺少有效输出, 跳过评审")
            return None

        # 匿名: 随机分配 A/B
        import random
        if random.random() < 0.5:
            report_a, label_a = result_single.output, "single"
            report_b, label_b = result_swarm.output, "swarm"
        else:
            report_a, label_a = result_swarm.output, "swarm"
            report_b, label_b = result_single.output, "single"

        # 截断避免超 token
        MAX_REPORT_LEN = 12000
        report_a_trunc = report_a[:MAX_REPORT_LEN] + ("... [截断]" if len(report_a) > MAX_REPORT_LEN else "")
        report_b_trunc = report_b[:MAX_REPORT_LEN] + ("... [截断]" if len(report_b) > MAX_REPORT_LEN else "")

        judge_prompt = textwrap.dedent(f"""\
            你是一位资深代码审计专家。请对以下两份关于 "{scenario_name}" 的分析报告进行盲评。

            ═══ Report A ═══
            {report_a_trunc}

            ═══ Report B ═══
            {report_b_trunc}

            请从以下 5 个维度分别打分 (1-10)：
            1. **completeness** — 覆盖全面性：是否覆盖了所有要求的分析维度？
            2. **depth** — 分析深度：是否深入到具体代码层面？
            3. **actionability** — 可操作性：修复建议是否具体可执行？
            4. **accuracy** — 引用准确性：文件名、行号、代码片段引用是否正确？
            5. **structure** — 结构清晰度：报告是否组织良好、易于阅读？

            请严格按以下 JSON 格式输出（不要加 markdown 代码块标记）：
            {{
                "report_a": {{
                    "completeness": <int>,
                    "depth": <int>,
                    "actionability": <int>,
                    "accuracy": <int>,
                    "structure": <int>
                }},
                "report_b": {{
                    "completeness": <int>,
                    "depth": <int>,
                    "actionability": <int>,
                    "accuracy": <int>,
                    "structure": <int>
                }},
                "winner": "A" | "B" | "tie",
                "rationale": "<简要说明为什么这份更好>"
            }}
        """)

        print("\n  🧑‍⚖️ LLM-as-Judge 评审中...")
        try:
            r = requests.post(
                f"{self.server}/api/chat/start",
                json={
                    "conversationId": f"judge_{int(time.time())}",
                    "message": judge_prompt,
                },
                timeout=30,
            )
            r.raise_for_status()
            judge_task_id = r.json().get("taskId") or r.json().get("task_id")

            # 等待 judge 完成 (纯文本任务, 不需要项目/工具, 应该较快)
            _, judge_output, _, _ = self._poll_until_done(
                judge_task_id, time.time(), "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            )
            print()  # 换行

            if not judge_output:
                print("  ⚠️ Judge 无输出")
                return None

            # 解析 JSON
            json_match = re.search(r'\{[\s\S]*\}', judge_output)
            if not json_match:
                print(f"  ⚠️ Judge 输出无法解析为 JSON")
                print(f"     输出前 200 字: {judge_output[:200]}")
                return None

            scores = json.loads(json_match.group())

            # 映射回 single/swarm
            score_a = scores.get("report_a", {})
            score_b = scores.get("report_b", {})
            winner_raw = scores.get("winner", "tie")

            if winner_raw == "A":
                winner = label_a
            elif winner_raw == "B":
                winner = label_b
            else:
                winner = "tie"

            single_scores = score_a if label_a == "single" else score_b
            swarm_scores = score_a if label_a == "swarm" else score_b

            def calc_total(s):
                return (s.get("completeness", 0) * 0.25 +
                        s.get("depth", 0) * 0.25 +
                        s.get("actionability", 0) * 0.2 +
                        s.get("accuracy", 0) * 0.15 +
                        s.get("structure", 0) * 0.15)

            return JudgeScore(
                completeness=single_scores.get("completeness", 0),
                depth=single_scores.get("depth", 0),
                actionability=single_scores.get("actionability", 0),
                accuracy=single_scores.get("accuracy", 0),
                structure=single_scores.get("structure", 0),
                total=calc_total(single_scores),
                rationale=scores.get("rationale", ""),
                winner=winner,
            )

        except json.JSONDecodeError as e:
            print(f"  ⚠️ JSON 解析失败: {e}")
            return None
        except Exception as e:
            print(f"  ⚠️ Judge 评审失败: {e}")
            return None

    # ────────────────────────────────────────
    #  场景执行
    # ────────────────────────────────────────

    def run_scenario(self, scenario: dict) -> dict:
        """执行一个场景的 single vs swarm 对比"""

        name = scenario["name"]
        prompt = scenario["prompt"]
        needs_project = scenario.get("needs_project", False)

        print(f"\n{'═' * 60}")
        print(f"  场景: {name}")
        print(f"  描述: {scenario['description']}")
        print(f"  项目: {self.project_path}")
        print(f"{'═' * 60}")

        # ── 1) Single agent ──
        print(f"\n{'─' * 40}")
        print(f"  ① Single Agent 模式")
        print(f"{'─' * 40}")
        result_single = self.run_task(prompt, swarm_enabled=False,
                                       scenario_name=name,
                                       needs_project=needs_project)
        self.results.append(result_single)

        # 短暂间隔避免速率限制
        print("\n  ⏸️  间隔 5s 避免速率限制...")
        time.sleep(5)

        # ── 2) Swarm agent ──
        print(f"\n{'─' * 40}")
        print(f"  ② Swarm Agent 模式")
        print(f"{'─' * 40}")
        result_swarm = self.run_task(prompt, swarm_enabled=True,
                                      scenario_name=name,
                                      needs_project=needs_project)
        self.results.append(result_swarm)

        return {
            "scenario": name,
            "description": scenario["description"],
            "single": asdict(result_single),
            "swarm": asdict(result_swarm),
        }

    # ────────────────────────────────────────
    #  汇总打印
    # ────────────────────────────────────────

    def print_summary(self, all_results: list):
        """打印汇总表格"""
        print(f"\n\n{'═' * 70}")
        print(f"  📊 测试汇总")
        print(f"{'═' * 70}")

        for r in all_results:
            s = r.get("single", {})
            w = r.get("swarm", {})
            j = r.get("judge")

            print(f"\n  场景: {r['scenario']}")
            print(f"  {'─' * 50}")
            print(f"  {'指标':<20} {'Single':<20} {'Swarm':<20}")
            print(f"  {'─' * 50}")
            print(f"  {'耗时 (s)':<20} {s.get('elapsed',0):>8.1f}{'':>12} {w.get('elapsed',0):>8.1f}")
            print(f"  {'输出字数':<18} {s.get('char_count',0):>8,}{'':>12} {w.get('char_count',0):>8,}")
            print(f"  {'章节数':<20} {s.get('section_count',0):>8}{'':>12} {w.get('section_count',0):>8}")
            print(f"  {'代码块数':<18} {s.get('code_block_count',0):>8}{'':>12} {w.get('code_block_count',0):>8}")
            print(f"  {'工具轮数':<18} {s.get('tool_rounds',0):>8}{'':>12} {w.get('tool_rounds',0):>8}")
            print(f"  {'Agent 数':<18} {'—':>8}{'':>12} {w.get('agent_count',0):>8}")

            if s.get('elapsed', 0) > 0 and w.get('elapsed', 0) > 0:
                speedup = s['elapsed'] / w['elapsed']
                print(f"  {'加速比':<20} {'':>20} {speedup:>7.2f}x")

            if j:
                print(f"\n  🧑‍⚖️ LLM 评审:")
                print(f"     胜者: {j.get('winner', '?')}")
                print(f"     理由: {j.get('rationale', '')[:100]}")

            if s.get('error'):
                print(f"\n  ❌ Single 错误: {s['error'][:100]}")
            if w.get('error'):
                print(f"\n  ❌ Swarm 错误: {w['error'][:100]}")


# ─── 保存结果 ──────────────────────────────

def save_results(all_results: list, path: str):
    """保存 JSON 结果"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  💾 结果已保存: {path}")


def save_report_markdown(all_results: list, path: str):
    """生成可读的 Markdown 报告"""
    lines = [
        "# Swarm vs Single Agent 对比报告",
        f"\n> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    for r in all_results:
        s = r.get("single", {})
        w = r.get("swarm", {})
        j = r.get("judge")

        lines.append(f"## {r['scenario']}")
        lines.append(f"\n{r.get('description', '')}\n")
        lines.append("| 指标 | Single | Swarm |")
        lines.append("|------|--------|-------|")
        lines.append(f"| 耗时 (s) | {s.get('elapsed',0):.1f} | {w.get('elapsed',0):.1f} |")
        lines.append(f"| 输出字数 | {s.get('char_count',0):,} | {w.get('char_count',0):,} |")
        lines.append(f"| 章节数 | {s.get('section_count',0)} | {w.get('section_count',0)} |")
        lines.append(f"| 代码块数 | {s.get('code_block_count',0)} | {w.get('code_block_count',0)} |")
        lines.append(f"| 工具轮数 | {s.get('tool_rounds',0)} | {w.get('tool_rounds',0)} |")
        lines.append(f"| Agent 数 | — | {w.get('agent_count',0)} |")

        if s.get('elapsed', 0) > 0 and w.get('elapsed', 0) > 0:
            speedup = s['elapsed'] / w['elapsed']
            lines.append(f"| **加速比** | — | **{speedup:.2f}x** |")

        if j:
            lines.append(f"\n### 🧑‍⚖️ LLM 评审")
            lines.append(f"\n- **胜者**: {j.get('winner', '?')}")
            lines.append(f"- **理由**: {j.get('rationale', '')}")

        # 折叠产出摘要
        for label, key in [("Single", "single"), ("Swarm", "swarm")]:
            output_text = r.get(key, {}).get("output", "")
            if output_text:
                preview = output_text[:2000].replace("\n", "\n> ")
                lines.append(f"\n<details><summary>{label} 产出摘要 ({len(output_text):,} chars)</summary>\n")
                lines.append(f"> {preview}")
                if len(output_text) > 2000:
                    lines.append(f"\n> ... [截断, 共 {len(output_text):,} 字符]")
                lines.append("\n</details>")

        # 事件时间线
        for label, key in [("Single", "single"), ("Swarm", "swarm")]:
            events = r.get(key, {}).get("events_summary", [])
            if events:
                lines.append(f"\n<details><summary>{label} 事件时间线 ({len(events)} events)</summary>\n")
                lines.append("```")
                for e in events[:50]:
                    lines.append(e)
                if len(events) > 50:
                    lines.append(f"... [共 {len(events)} 事件]")
                lines.append("```")
                lines.append("\n</details>")

        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  📝 Markdown 报告: {path}")


# ─── main ──────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Swarm vs Single Agent 对比基准测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例:
              python debug/bench_swarm_vs_single.py
              python debug/bench_swarm_vs_single.py --scenario 0
              python debug/bench_swarm_vs_single.py --no-judge
              python debug/bench_swarm_vs_single.py --server http://10.0.0.1:15000
        """),
    )
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help=f"服务器地址 (default: {DEFAULT_SERVER})")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="指定模型 (空=使用服务器默认)")
    parser.add_argument("--scenario", type=int, default=None,
                        help="只运行指定场景 (0-based index)")
    parser.add_argument("--no-judge", action="store_true",
                        help="跳过 LLM-as-Judge 评审")
    parser.add_argument("--output", default="",
                        help="输出 JSON 路径")
    parser.add_argument("--project", default="",
                        help="项目路径 (默认=当前目录)")

    args = parser.parse_args()

    # 推断项目路径
    project_path = args.project or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    bench = SwarmBenchmark(
        server_url=args.server,
        model=args.model,
        project_path=project_path,
    )

    # 服务器检查
    print(f"\n🔍 检查服务器: {bench.server}")
    if not bench.check_server():
        print("❌ 服务器不可用, 请检查地址")
        sys.exit(1)
    print("✅ 服务器就绪")

    # 选择场景
    if args.scenario is not None:
        if args.scenario < 0 or args.scenario >= len(SCENARIOS):
            print(f"❌ 场景索引 {args.scenario} 超出范围 (0-{len(SCENARIOS)-1})")
            sys.exit(1)
        scenarios = [SCENARIOS[args.scenario]]
    else:
        scenarios = SCENARIOS

    print(f"\n📋 将执行 {len(scenarios)} 个场景:")
    for i, s in enumerate(scenarios):
        print(f"  [{i}] {s['name']} — {s['description']}")

    # 执行
    all_results = []
    for scenario in scenarios:
        result = bench.run_scenario(scenario)

        # Judge
        if not args.no_judge:
            single_result = next((r for r in bench.results
                                  if r.mode == "single" and r.scenario == scenario["name"]),
                                 None)
            swarm_result = next((r for r in bench.results
                                 if r.mode == "swarm" and r.scenario == scenario["name"]),
                                None)
            if single_result and swarm_result:
                judge_result = bench.judge(single_result, swarm_result, scenario["name"])
                if judge_result:
                    result["judge"] = asdict(judge_result)

        all_results.append(result)

    # 汇总
    bench.print_summary(all_results)

    # 保存结果
    ts = int(time.time())
    json_path = args.output or f"debug/bench_swarm_result_{ts}.json"
    save_results(all_results, json_path)

    md_path = json_path.replace(".json", ".md")
    save_report_markdown(all_results, md_path)


if __name__ == "__main__":
    main()
