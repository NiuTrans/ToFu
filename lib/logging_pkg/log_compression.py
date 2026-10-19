"""Bounded application service for LLM-assisted log compression.

The HTTP adapter never invokes model dispatch directly. This owner supplies a
finite deadline, production retry budget, explicit principal, shared admission
lease, and ExecutionSession terminal receipt around the single model call.
"""

from __future__ import annotations

import re
import time

from lib.agent_core.admission import controller
from lib.agent_core.execution_session import (
    ExecutionPhase,
    ExecutionSession,
    acquire_and_bind_admission,
)
from lib.ids import short_id
from lib.production.llm_policy import production_llm_dispatch_kwargs


LOG_COMPRESSION_TIMEOUT_SECONDS = 120
LOG_COMPRESSION_MAX_INPUT_CHARS = 60_000


class LogCompressionBusyError(RuntimeError):
    pass


def compress_logs(text: str, *, owner_user_id: int) -> tuple[str, dict]:
    bounded_text = str(text or "").strip()
    if len(bounded_text) > LOG_COMPRESSION_MAX_INPUT_CHARS:
        bounded_text = (
            bounded_text[:LOG_COMPRESSION_MAX_INPUT_CHARS]
            + "\n... [truncated]"
        )

    session = ExecutionSession(
        execution_id=short_id("log-compress-", 20),
        kind="log_compression",
        owner_user_id=owner_user_id,
        deadline_seconds=LOG_COMPRESSION_TIMEOUT_SECONDS,
    )
    admission_lease = acquire_and_bind_admission(session, controller)
    if admission_lease is None:
        raise LogCompressionBusyError("log compression is at capacity")
    session.mark_dispatch_started()
    deadline = time.monotonic() + LOG_COMPRESSION_TIMEOUT_SECONDS

    system_prompt = (
        "你是一个**日志压缩器**。你的唯一任务是把冗长的日志/终端输出压缩为更精简的版本，同时不丢失任何有意义的信息。\n\n"
        "## 压缩规则（按优先级）\n"
        "1. **合并重复**：同一条消息因多个 worker/rank/GPU/进程而重复多次 → 只保留一条有代表性的，在行尾标注 `  ×N`\n"
        "   - 如果不同 rank 的值不同（如耗时、端口），保留一条代表值即可\n"
        "2. **去除纯噪音**：删除空行、纯分隔线、下载进度条和无关 DEBUG 噪音。\n"
        "3. **保留所有有意义的信息**：完整保留 ERROR、WARNING、关键 INFO、版本、模型和硬件信息。\n"
        "4. **去掉无意义的日志时间戳前缀**，但保留有分析价值的耗时信息。\n"
        "5. 直接输出压缩后的纯文本，不要代码块、解释、总结或标题。"
    )

    try:
        from lib.llm_dispatch import smart_chat

        content, usage = smart_chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": bounded_text},
            ],
            max_tokens=min(len(bounded_text) // 2 + 2000, 16_000),
            temperature=0,
            capability="cheap",
            log_prefix="[LogCompress]",
            timeout=LOG_COMPRESSION_TIMEOUT_SECONDS,
            owner_user_id=owner_user_id,
            abort_check=lambda: (
                session.cancel_requested or time.monotonic() >= deadline
            ),
            **production_llm_dispatch_kwargs(),
        )
        content = str(content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```[^\n]*\n", "", content)
            content = re.sub(r"\n```\s*$", "", content).strip()
        receipt = session.settle(ExecutionPhase.COMPLETED)
        if not receipt.invariants_satisfied:
            raise RuntimeError("log compression resource settlement failed")
        return content, dict(usage or {})
    except BaseException as exc:
        outcome = (
            ExecutionPhase.TIMED_OUT
            if session.cancel_requested or time.monotonic() >= deadline
            else ExecutionPhase.CANCELLED
            if isinstance(exc, (KeyboardInterrupt, SystemExit))
            else ExecutionPhase.FAILED
        )
        session.settle(outcome, cause=type(exc).__name__)
        raise


__all__ = [
    "LOG_COMPRESSION_MAX_INPUT_CHARS",
    "LOG_COMPRESSION_TIMEOUT_SECONDS",
    "LogCompressionBusyError",
    "compress_logs",
]
