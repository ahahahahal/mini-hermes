"""
记忆工具。

【架构】对应白皮书 03-自进化闭环 §6.4。让模型能主动把"值得长期记住的东西"
写进记忆 —— 这是"经验沉淀"里模型可主动触发的那一半。

注意与 skills 的分工：
    技能 = 怎么做某件事（程序性）
    记忆 = 关于用户/项目的事实（陈述性）
"""

from __future__ import annotations

from minihermes.memory import MemoryStore, default_memory_root
from minihermes.registry import register

_store: MemoryStore | None = None


def configure(store: MemoryStore) -> None:
    global _store
    _store = store


def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore(default_memory_root())
    return _store


def _memory_read(args: dict) -> str:
    scope = str(args.get("scope", "memory")).strip()
    try:
        content = get_store().read(scope)
    except ValueError as exc:
        return f"错误：{exc}"
    if not content:
        return f"（{scope} 目前是空的）"
    return content


def _memory_write(args: dict) -> str:
    scope = str(args.get("scope", "memory")).strip()
    content = args.get("content")
    if content is None:
        return "错误：缺少 content 参数。"
    try:
        return get_store().write(scope, str(content))
    except ValueError as exc:
        return f"错误：{exc}"


def _memory_append(args: dict) -> str:
    scope = str(args.get("scope", "memory")).strip()
    note = str(args.get("note", "")).strip()
    if not note:
        return "错误：note 不能为空。"
    try:
        return get_store().append(scope, note)
    except ValueError as exc:
        return f"错误：{exc}"


register(
    name="memory_read",
    toolset="memory",
    description="读取长期记忆或用户画像",
    emoji="🧠",
    schema={
        "name": "memory_read",
        "description": "读取指定作用域的记忆内容。scope='memory' 读长期记忆，scope='user' 读用户画像。",
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "记忆作用域，默认 memory",
                }
            },
            "required": [],
        },
    },
    handler=_memory_read,
)

register(
    name="memory_write",
    toolset="memory",
    description="整体覆盖写入长期记忆或用户画像",
    emoji="✍️",
    schema={
        "name": "memory_write",
        "description": (
            "整体替换某个作用域的记忆内容。用于整理与精简 —— 记忆每轮都会注入，"
            "体积就是持续成本，所以要定期合并过时条目。\n"
            "写入什么：稳定的事实、用户偏好、项目约定。\n"
            "不要写入：临时的中间结果、一次性的路径、会话里马上就会过期的东西。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["memory", "user"]},
                "content": {"type": "string", "description": "完整的记忆内容（整体覆盖）"},
            },
            "required": ["scope", "content"],
        },
    },
    handler=_memory_write,
)

register(
    name="memory_append",
    toolset="memory",
    description="往记忆里追加一条带时间戳的条目",
    emoji="➕",
    schema={
        "name": "memory_append",
        "description": "在指定作用域的末尾追加一条带时间戳的记录。适合随手记下零散但有用的事实。",
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["memory", "user"]},
                "note": {"type": "string", "description": "要记下的内容，一句话说清"},
            },
            "required": ["scope", "note"],
        },
    },
    handler=_memory_append,
)
