"""监控器：bounded 轮询画布 eval job 状态直到终态或超时。

只读、默认 60s 间隔、连续 ``max_consecutive_errors`` 次 observer error 即停（平台
读路径 504 是观测失败不是任务失败，但持续失败说明链路断了，继续盲等无意义）。

状态源是 ``experiment_eval_jobs``：list 模式给 taskId/status/model binding；``job_id=``
详情模式给 ``resourceConfigList``/``consumerList``/agent ``workers``。本组件通过
``JobsClient`` 协议注入（真实实现走 llm MCP；测试注入 stub）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

# 平台终态（evalFlowJobStatus / experiment_eval_jobs 的 status 语义）
TERMINAL_OK = frozenset({"DONE", "COMPLETED", "SUCCESS"})
TERMINAL_BAD = frozenset({"ERROR", "FAILED", "STOPPED", "KILLED", "CALLBACK_FAIL"})
TERMINAL_ALL = TERMINAL_OK | TERMINAL_BAD


class JobsClient(Protocol):
    """eval jobs 查询桥（真实实现走 llm MCP experiment_eval_jobs）。"""

    def status(self, flow_id: str) -> dict[str, Any]:
        """返回 {'status': <str>, ...}。status 取自平台 evalFlowJobStatus。"""
        ...


@dataclass(frozen=True)
class WatchResult:
    """监控终态。"""

    flow_id: str
    status: str
    ok: bool  # status in TERMINAL_OK
    polls: int
    elapsed_sec: float
    stop_reason: str  # "terminal" | "observer_errors" | "deadline"


def watch_flow(
    client: JobsClient,
    flow_id: str,
    *,
    interval_sec: float = 60.0,
    max_consecutive_errors: int = 3,
    deadline_sec: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    on_poll: Callable[[int, dict[str, Any]], None] | None = None,
) -> WatchResult:
    """轮询 flow 状态直到终态 / 连续 observer error / deadline。

    ``deadline_sec=None`` 表示不设总截止（TB2.1 全量 445 trial 可能跑数小时，由
    调用方决定上限）。注入 ``sleep``/``monotonic`` 便于离线测试。
    """
    start = monotonic()
    polls = 0
    consecutive_errors = 0
    last: dict[str, Any] = {}

    while True:
        polls += 1
        try:
            last = client.status(flow_id)
            consecutive_errors = 0
        except Exception:  # observer error（平台读路径 504 等）
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                return WatchResult(
                    flow_id=flow_id,
                    status="OBSERVER_ERROR",
                    ok=False,
                    polls=polls,
                    elapsed_sec=monotonic() - start,
                    stop_reason="observer_errors",
                )
            sleep(interval_sec)
            continue

        if on_poll is not None:
            on_poll(polls, last)

        status = str(last.get("status", "")).upper()
        if status in TERMINAL_ALL:
            return WatchResult(
                flow_id=flow_id,
                status=status,
                ok=status in TERMINAL_OK,
                polls=polls,
                elapsed_sec=monotonic() - start,
                stop_reason="terminal",
            )

        if deadline_sec is not None and (monotonic() - start) >= deadline_sec:
            return WatchResult(
                flow_id=flow_id,
                status=status or "UNKNOWN",
                ok=False,
                polls=polls,
                elapsed_sec=monotonic() - start,
                stop_reason="deadline",
            )

        sleep(interval_sec)
