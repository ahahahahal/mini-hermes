"""
minihermes.memory —— 记忆系统（情景/语义记忆的最简实现）

【架构地位】对应白皮书 03-自进化闭环 §6。真实 Hermes 有 8 种记忆后端
（honcho / mem0 / supermemory / ...），统一在 MemoryProvider 抽象之下。
这里只保留"文件即记忆"这一种最朴素、也最容易理解的实现。

【与技能相同的铁律】
    记忆内容同样作为 **user message** 注入，不进 system prompt。
    理由见 skills.py 顶部说明：保前缀缓存。

【两种作用域】
    memory  —— 项目/事实性长期记忆（MEMORY.md）
    user    —— 用户画像与偏好（USER.md）
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

SCOPES = {
    "memory": "MEMORY.md",
    "user": "USER.md",
}

# 单个作用域的内容上限。记忆是"注入到每轮对话"的东西，
# 不设上限会稳步吃掉上下文预算 —— 这是很多自建 agent 的死法。
MAX_CHARS = 8_000


def default_memory_root() -> Path:
    return Path(os.environ.get("MINIHERMES_HOME", Path.home() / ".minihermes")) / "memory"


class MemoryStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_memory_root()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, scope: str) -> Path:
        if scope not in SCOPES:
            raise ValueError(f"未知记忆作用域 {scope!r}，可选：{', '.join(SCOPES)}")
        return self.root / SCOPES[scope]

    # ------------------------------------------------------------------ 读

    def read(self, scope: str) -> str:
        path = self._path(scope)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def context_block(self) -> str:
        """拼出注入用的记忆块。空记忆返回空串（不占 token）。"""
        chunks: list[str] = []
        mem = self.read("memory")
        if mem:
            chunks.append(f"## 长期记忆\n{mem}")
        usr = self.read("user")
        if usr:
            chunks.append(f"## 用户画像\n{usr}")
        return "\n\n".join(chunks)

    # ------------------------------------------------------------------ 写

    def write(self, scope: str, content: str) -> str:
        """整体覆盖某个作用域。超限则拒绝，而不是静默截断。"""
        path = self._path(scope)
        text = content.strip()
        if len(text) > MAX_CHARS:
            return (
                f"错误：{SCOPES[scope]} 内容将达 {len(text)} 字符，超过 {MAX_CHARS} 上限。"
                "记忆需要被精简 —— 请先删掉过时或无用的条目再写入（记忆是每轮都要注入的，"
                "体积直接等于持续成本）。"
            )
        path.write_text(text + "\n", encoding="utf-8")
        return f"✅ 已写入 {path}（{len(text)} 字符）"

    def append(self, scope: str, note: str) -> str:
        """追加一条带时间戳的条目。"""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        existing = self.read(scope)
        line = f"- [{stamp}] {note.strip()}"
        merged = f"{existing}\n{line}" if existing else line
        return self.write(scope, merged)
