"""
minihermes.context —— 上下文压缩

【架构】对应白皮书 01-Agent内核 §6。

这是**唯一被允许改写对话历史的时刻**。Hermes 的 prompt 缓存铁律（白皮书铁律一）
之所以允许这一个例外，是因为不压缩就必然超上下文窗口，那是硬失败。

【最容易写错的地方：不要拆散 tool_call / tool_result 配对】
OpenAI 格式里，带 tool_calls 的 assistant 消息，后面**必须**紧跟一一对应的
role="tool" 消息。如果压缩时把 assistant 留下、把 tool 结果删掉（或反过来），
下一次请求会被服务端直接拒绝。

所以本模块不按"单条消息"切，而是按**完整回合**切：
每个回合 = 一条 user 消息 + 其后的 assistant / tool 消息，直到下一条 user 为止。
压缩的单位是回合，配对自然不会被破坏。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

Message = dict[str, Any]

# 粗估：ASCII 约 3.5 字符 / token，CJK 约 1 字符 / token。
# 不追求精确 —— 目的是留出安全余量，不是做计费。
_ASCII_PER_TOKEN = 3.5


def estimate_tokens(messages: list[Message]) -> int:
    total = 0
    for msg in messages:
        total += 4  # 每条消息的固定结构开销
        total += _text_tokens(str(msg.get("content") or ""))
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            total += _text_tokens(str(fn.get("name") or ""))
            total += _text_tokens(str(fn.get("arguments") or ""))
    return int(total)


def _text_tokens(text: str) -> float:
    if not text:
        return 0.0
    ascii_n = 0
    for ch in text:
        if ord(ch) < 128:
            ascii_n += 1
    other = len(text) - ascii_n
    return ascii_n / _ASCII_PER_TOKEN + other * 1.0


# --------------------------------------------------------------------- 切分

def segment_turns(messages: list[Message]) -> list[list[Message]]:
    """把消息切成"回合"，保证 assistant(tool_calls) 与其 tool 结果不被分开。"""
    segments: list[list[Message]] = []
    current: list[Message] = []

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            # system 不参与回合切分，由调用方单独保留
            continue
        if role == "user" and current:
            segments.append(current)
            current = [msg]
        else:
            current.append(msg)
    if current:
        segments.append(current)
    return segments


# --------------------------------------------------------------------- 压缩器

@dataclass
class CompressionResult:
    messages: list[Message]
    compressed: bool
    dropped_turns: int = 0
    note: str = ""


class ContextCompressor:
    def __init__(self, max_tokens: int = 32_000, keep_recent_turns: int = 4) -> None:
        self.max_tokens = max_tokens
        self.keep_recent_turns = keep_recent_turns

    def needs_compression(self, messages: list[Message]) -> bool:
        return estimate_tokens(messages) > self.max_tokens

    def compress(self, messages: list[Message]) -> CompressionResult:
        if not self.needs_compression(messages):
            return CompressionResult(messages=messages, compressed=False)

        system_msgs = [m for m in messages if m.get("role") == "system"]
        turns = segment_turns(messages)

        # 回合太少就没得压 —— 再压会把当前任务本身干掉。
        # 宁可带着超长上下文去请求，也不要压到模型看不懂它在干什么。
        if len(turns) <= self.keep_recent_turns + 1:
            return CompressionResult(
                messages=messages,
                compressed=False,
                note="回合数不足以压缩，跳过。",
            )

        head = turns[0]                       # 首个回合 = 原始任务，必须留
        tail = turns[-self.keep_recent_turns:]  # 最近几轮 = 当前工作状态，必须留
        middle = turns[1 : len(turns) - self.keep_recent_turns]

        digest = _summarize(middle)
        rebuilt = list(system_msgs) + list(head) + [digest] + [
            m for turn in tail for m in turn
        ]

        return CompressionResult(
            messages=rebuilt,
            compressed=True,
            dropped_turns=len(middle),
            note=(
                f"已压缩 {len(middle)} 个回合（约 {estimate_tokens(messages)} → "
                f"{estimate_tokens(rebuilt)} tokens）。"
            ),
        )


def _summarize(turns: list[list[Message]]) -> Message:
    """把若干回合压成一条摘要消息。

    真实 Hermes 会调一次 LLM 做摘要（更贵但更保真）。这里用规则摘要，
    理由是最小内核不该在压缩里再引入一次模型调用 —— 那会让压缩本身
    也可能失败、也需要重试。规则摘要丢掉的是细节，保住的是"发生过什么"。
    """
    tool_names: list[str] = []
    user_snippets: list[str] = []
    last_assistant = ""

    for turn in turns:
        for msg in turn:
            role = msg.get("role")
            if role == "user":
                snippet = _clip(str(msg.get("content") or ""), 120)
                if snippet:
                    user_snippets.append(snippet)
            elif role == "assistant":
                for call in msg.get("tool_calls") or []:
                    name = (call.get("function") or {}).get("name")
                    if name and name not in tool_names:
                        tool_names.append(str(name))
                if msg.get("content"):
                    last_assistant = _clip(str(msg["content"]), 300)

    lines = [f"[上下文已压缩] 此前的 {len(turns)} 个回合已被折叠。"]
    if user_snippets:
        lines.append("当时用户的诉求（节选）：")
        lines.extend(f"  · {s}" for s in user_snippets[:6])
    if tool_names:
        lines.append(f"用过的工具：{', '.join(tool_names)}")
    if last_assistant:
        lines.append(f"上次的结论（节选）：{last_assistant}")
    lines.append("如需细节，请重新读取相关文件或重新执行命令，不要凭印象编造。")

    return {"role": "user", "content": "\n".join(lines)}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"
