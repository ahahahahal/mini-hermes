"""
minihermes.provider —— 模型接入层

【架构】对应白皮书 04-网关与插件 §11 与 01-Agent内核 §7。
内核只依赖一个极窄的协议：给 messages + tools，拿回 assistant 消息。

两个实现：

  * OpenAICompatProvider —— 走 HTTP 打 /chat/completions，任何 OpenAI 兼容端点都能用
                            （OpenRouter / DeepSeek / Moonshot / 本地 vLLM / ollama...）
  * MockProvider        —— 零网络的假模型。用途不是"凑数"，而是让整个内核
                            可以在没有 API key、没有网络的情况下被端到端验证。

【有意简化】
  真实 Hermes 优先走**流式**，因为流式即便不需要流式输出，也能提供
  90 秒停滞检测与 60 秒读超时这类细粒度健康检查，非流式路径没有。
  这里为保持内核可读，只实现非流式；超时靠 urllib 的 timeout 兜底。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


# --------------------------------------------------------------------- 数据结构

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # 原始 JSON 字符串（保持字符串形态，便于容错解析）


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    """模型调用失败。status_code 供错误分类器判断要不要重试。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Provider(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...


# --------------------------------------------------------------------- 真实后端

class OpenAICompatProvider:
    """OpenAI 兼容的 chat/completions 后端（纯标准库 urllib 实现）。"""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 120,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("MINIHERMES_API_KEY", "")
        self.base_url = (base_url or os.environ.get("MINIHERMES_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.timeout = timeout

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(
                f"HTTP {exc.code}: {detail}", status_code=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            # 网络层失败：没有 status_code，交给分类器按"可重试"处理
            raise LLMError(f"网络错误: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"响应不是合法 JSON: {exc}") from exc

        return parse_response(body)


def parse_response(body: dict[str, Any]) -> LLMResponse:
    """把 OpenAI 格式的响应体转成 LLMResponse。"""
    choices = body.get("choices") or []
    if not choices:
        raise LLMError(f"响应中没有 choices: {json.dumps(body)[:300]}")
    choice = choices[0]
    message = choice.get("message") or {}

    calls: list[ToolCall] = []
    for idx, tc in enumerate(message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        calls.append(
            ToolCall(
                id=str(tc.get("id") or f"call_{idx}"),
                name=str(fn.get("name") or ""),
                arguments=str(fn.get("arguments") or "{}"),
            )
        )

    return LLMResponse(
        content=message.get("content"),
        tool_calls=calls,
        finish_reason=str(choice.get("finish_reason") or "stop"),
        raw=body,
    )


# --------------------------------------------------------------------- 假后端

class MockProvider:
    """零网络假模型：按预设脚本依次返回响应。

    script 里的元素可以是：
      * LLMResponse    —— 直接返回
      * Exception      —— 抛出（用来验证重试/降级逻辑）
      * dict           —— 会被当作 OpenAI 响应体解析（更贴近真实路径）

    脚本用完后，返回一个用于收尾的普通文本响应，让主循环自然结束。
    """

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[dict[str, Any]] = []  # 供测试断言
        self.exhausted_note = "（mock 脚本已耗尽，返回最终答复）"

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools})

        if not self.script:
            return LLMResponse(content=self.exhausted_note, finish_reason="stop")

        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, LLMResponse):
            return nxt
        if isinstance(nxt, dict):
            return parse_response(nxt)
        return LLMResponse(content=str(nxt), finish_reason="stop")


# --------------------------------------------------------------------- 便捷构造

def tool_call(tool_name: str, /, **kwargs: Any) -> ToolCall:
    """测试/脚本里快速造一个工具调用。

    第一个参数声明为"仅位置参数"（`/`），这样当工具自身也有名为 name 的参数时，
    `tool_call("skill_manage", name="x")` 不会撞车。
    """
    return ToolCall(id=f"call_{tool_name}", name=tool_name, arguments=json.dumps(kwargs))


def build_provider(model: str | None = None, **kwargs: Any) -> Provider:
    """按环境变量挑后端：没配 API key 就自动落到 Mock，保证任何时候都能跑。"""
    model = model or os.environ.get("MINIHERMES_MODEL", "gpt-4o-mini")
    api_key = kwargs.pop("api_key", None) or os.environ.get("MINIHERMES_API_KEY", "")
    if not api_key:
        return MockProvider()
    return OpenAICompatProvider(model=model, api_key=api_key, **kwargs)
