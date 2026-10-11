"""
minihermes.loop —— 主循环

【架构】这是内核真正的心脏，对应白皮书 01-Agent内核 §2 的
`agent/conversation_loop.py::run_conversation`（Hermes 里那是一个约 4000 行的单函数）。

这里把它压到几百行，但保留了全部关键分支：

    1. 上下文注入 —— 记忆等动态内容拼进 **user message**，system prompt 恒定
    2. 中断检查 —— 循环头、API 调用后、每个工具执行前，三个响应点
    3. 错误处理 —— 分类 → 退避重试 → 压缩重试 → 降级模型
    4. 压缩检查 —— 每轮开始前按需压缩（唯一允许改写历史的地方）
    5. 预算控制 —— 消耗、退还（廉价工具）、耗尽后的一次 grace call
    6. 工具分发 —— 参数解析容错，坏参数变成给模型的错误文本而不是崩溃
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from minihermes.budget import CHEAP_TOOLSETS, IterationBudget
from minihermes.callbacks import Callbacks, null_callbacks
from minihermes.context import ContextCompressor
from minihermes.errors import (
    ErrorKind,
    backoff_seconds,
    classify,
    should_compress,
    should_fallback,
    should_retry,
)
from minihermes.provider import LLMResponse, Provider, ToolCall
from minihermes.registry import registry as default_registry

logger = logging.getLogger("minihermes.loop")

Message = dict[str, Any]

# 预算耗尽时的收尾提示。注意它是作为 user message 注入的（保系统提示恒定）。
_GRACE_HINT = (
    "[系统提示] 迭代预算已耗尽。请基于目前已经获得的信息，直接给出最终答复，"
    "不要再调用任何工具。如果任务未完成，请明确说明已完成到哪一步、剩下什么。"
)


@dataclass
class TurnResult:
    text: str
    iterations: int
    stop_reason: str  # completed | budget_exhausted | interrupted | error
    error: str = ""
    compressed: bool = False

    @property
    def completed(self) -> bool:
        return self.stop_reason == "completed"


class ConversationLoop:
    def __init__(
        self,
        provider: Provider,
        system_prompt: str,
        *,
        tools=None,
        compressor: ContextCompressor | None = None,
        budget: IterationBudget | None = None,
        callbacks: Callbacks | None = None,
        max_retries: int = 3,
        fallback_provider: Provider | None = None,
        context_injector: Callable[[], str] | None = None,
        model: str = "",
    ) -> None:
        self.provider = provider
        self.fallback_provider = fallback_provider
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools if tools is not None else default_registry
        self.compressor = compressor or ContextCompressor()
        self.budget = budget or IterationBudget()
        self.callbacks = callbacks or null_callbacks()
        self.max_retries = max_retries
        self.context_injector = context_injector

        self._history: list[Message] = []
        self._interrupted = threading.Event()
        self._grace_used = False

        # 工具定义在会话开始时取一次并缓存。
        # 这与 Hermes 的缓存铁律同源：**会话中途改变工具集会破坏 prompt 缓存**，
        # 所以工具箱在会话生命周期内必须保持稳定。
        self._tool_defs = self.tools.get_definitions()

    # ------------------------------------------------------------------ 控制

    def interrupt(self) -> None:
        """请求中断当前回合。内核会在下一个响应点退出。"""
        self._interrupted.set()

    def reset(self) -> None:
        """清空对话历史（保留 system prompt 与预算）。"""
        self._history.clear()
        self._interrupted.clear()
        self._grace_used = False

    def inject(self, text: str) -> None:
        """往历史里注入一条 user 消息。

        用于软引导（如"考虑沉淀技能"）与技能正文注入。
        ⚠️ 之所以走 user 消息而不是改 system prompt —— 见 _wrap_user_input 的说明。
        """
        self._history.append({"role": "user", "content": text})

    @property
    def history(self) -> list[Message]:
        return list(self._history)

    # ------------------------------------------------------------------ 组装

    def _assembled(self) -> list[Message]:
        """system + 历史。system 永远在最前且内容恒定。"""
        return [{"role": "system", "content": self.system_prompt}] + self._history

    def _wrap_user_input(self, user_text: str) -> str:
        """把动态上下文拼进 user message 的内容里。

        ⚠️ 这是 prompt 缓存铁律在代码层面的落点：
        记忆、检索结果这类"每轮可能不同"的东西，**绝不能**拼进 system prompt。
        拼进 system prompt 会让每一轮的前缀缓存全部失效。
        """
        if self.context_injector is None:
            return user_text
        try:
            block = self.context_injector()
        except Exception:
            logger.warning("上下文注入失败，按无注入处理", exc_info=True)
            return user_text
        if not block:
            return user_text
        return f"{block}\n\n---\n\n{user_text}"

    # ------------------------------------------------------------------ 主循环

    def run(self, user_text: str) -> TurnResult:
        self._interrupted.clear()
        self._grace_used = False
        self._history.append({"role": "user", "content": self._wrap_user_input(user_text)})

        iterations = 0
        compressed_any = False

        while True:
            # ---- 响应点 1：循环头
            if self._interrupted.is_set():
                return TurnResult("", iterations, "interrupted")

            # ---- 压缩检查（唯一允许改写历史的时刻）
            if self.compressor.needs_compression(self._assembled()):
                res = self.compressor.compress(self._assembled())
                if res.compressed:
                    self._history = [m for m in res.messages if m.get("role") != "system"]
                    compressed_any = True
                    self.callbacks.emit("on_status", f"上下文压缩：{res.note}")

            # ---- 预算检查 + grace call
            if self.budget.exhausted:
                if self._grace_used:
                    return TurnResult(
                        "达到迭代预算上限，已停止。",
                        iterations,
                        "budget_exhausted",
                        compressed=compressed_any,
                    )
                self._grace_used = True
                self._history.append({"role": "user", "content": _GRACE_HINT})
                self.callbacks.emit("on_status", "预算耗尽，给予最后一次机会")

            # ---- 调用模型
            try:
                resp = self._call_with_retries()
            except Exception as exc:
                self.callbacks.emit("on_error", exc, f"模型调用最终失败：{exc}")
                return TurnResult("", iterations, "error", error=str(exc))

            # ---- 响应点 2：API 调用返回后
            if self._interrupted.is_set():
                return TurnResult("", iterations, "interrupted")

            self._history.append(_assistant_message(resp))

            # ---- 没有工具调用 → 本轮结束
            if not resp.tool_calls:
                if resp.content:
                    self.callbacks.emit("on_assistant_text", resp.content)
                return TurnResult(
                    resp.content or "",
                    iterations,
                    "completed",
                    compressed=compressed_any,
                )

            # ---- 执行工具
            iterations += 1
            self.budget.consume()
            self.callbacks.emit("on_budget", self.budget)

            cheap_only = True
            for call in resp.tool_calls:
                # ---- 响应点 3：每个工具执行前
                if self._interrupted.is_set():
                    return TurnResult("", iterations, "interrupted")

                if self.tools.toolset_of(call.name) not in CHEAP_TOOLSETS:
                    cheap_only = False
                result = self._execute_tool(call)
                self._history.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )

            # 本轮只调了廉价工具 → 退还预算。
            # 否则一个本该 1 轮的"批量读技能"会被当成 N 轮烧掉额度。
            if cheap_only and resp.tool_calls:
                self.budget.refund()

            if self._interrupted.is_set():
                return TurnResult("", iterations, "interrupted")

    # ------------------------------------------------------------------ 工具

    def _execute_tool(self, call: ToolCall) -> str:
        self.callbacks.emit("on_tool_start", call.name, _safe_args(call.arguments))
        args, err = _parse_args(call.arguments)
        if err is not None:
            result = err
        else:
            result = self.tools.dispatch(call.name, args)
        self.callbacks.emit("on_tool_end", call.name, result)
        return result

    # ------------------------------------------------------------------ 容错

    def _call_with_retries(self) -> LLMResponse:
        """分类 → 重试 → 压缩 → 降级。这是错误处理的唯一入口。"""
        attempt = 0
        provider = self.provider

        while True:
            attempt += 1
            try:
                return provider.complete(self._assembled(), self._tool_defs)
            except Exception as exc:
                kind = classify(exc)
                self.callbacks.emit(
                    "on_error", exc, f"[{kind.value}] 第 {attempt} 次尝试失败：{exc}"
                )

                # ① 上下文超限 → 强制压缩后重试（不算重试次数）
                if should_compress(kind):
                    res = self.compressor.compress(self._assembled())
                    if res.compressed:
                        self._history = [
                            m for m in res.messages if m.get("role") != "system"
                        ]
                        continue
                    # 压不动了，继续往下降级/重试逻辑走

                # ② 可重试 → 退避后再试
                if should_retry(kind, attempt, self.max_retries):
                    wait = backoff_seconds(attempt)
                    self.callbacks.emit("on_status", f"重试中（{wait:.0f}s 后）")
                    time.sleep(wait)
                    continue

                # ③ 凭证/致命问题 → 降级到备用模型
                if should_fallback(kind) and self.fallback_provider is not None and provider is not self.fallback_provider:
                    self.callbacks.emit("on_status", "主模型不可用，降级到备用模型")
                    provider = self.fallback_provider
                    attempt = 0
                    continue

                raise


# --------------------------------------------------------------------- 辅助

def _assistant_message(resp: LLMResponse) -> Message:
    """把 LLMResponse 转成可回填进历史的 assistant 消息。

    content 为 None 时补成空串 —— 有些服务端要求字段存在。
    """
    msg: Message = {"role": "assistant", "content": resp.content or ""}
    if resp.tool_calls:
        msg["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments},
            }
            for c in resp.tool_calls
        ]
    return msg


def _parse_args(raw: str) -> tuple[dict, str | None]:
    """解析工具参数。坏 JSON 不抛异常，而是变成给模型看的错误文本。

    理由：模型偶尔会产出截断或多余的 JSON。把错误如实告诉它、让它重试，
    比让整个会话崩掉好得多。
    """
    if not raw or not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, (
            f"错误：工具参数不是合法 JSON（{exc.msg}）。"
            f"你发送的内容是：{raw[:200]}。请重新生成正确的 JSON 参数。"
        )
    if not isinstance(parsed, dict):
        return {}, f"错误：工具参数必须是 JSON 对象，收到的是 {type(parsed).__name__}。"
    return parsed, None


def _safe_args(raw: str) -> dict:
    args, err = _parse_args(raw)
    return args if err is None else {"__raw__": raw}
