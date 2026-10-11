"""
minihermes.agent —— 组装层

把 provider / 工具注册表 / 技能 / 记忆 / 主循环装成一个可用的 Agent。

【架构地位】这是"编排层"里最薄的一层。它只做三件事：
  1. 构建 system prompt（恒定部分：人设 + 技能清单）
  2. 决定动态上下文如何注入（记忆 → user message）
  3. 回合结束后按计数触发"软引导"复盘提示

【软 vs 硬 —— 这是 Hermes 自我改进机制里最值得学的一点】
    触发**判定**是硬的：迭代计数到了就一定会触发（确定性代码）。
    但"要不要真的建技能"是软的：只提示模型考虑，不强制它建。

    很多人做自进化 agent 时反过来 —— 让模型自己决定什么时候复盘（软的判定），
    结果就是它永远不复盘。判定必须硬，动作可以软。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from minihermes.budget import IterationBudget
from minihermes.callbacks import Callbacks, null_callbacks
from minihermes.context import ContextCompressor
from minihermes.loop import ConversationLoop, TurnResult
from minihermes.memory import MemoryStore, default_memory_root
from minihermes.provider import Provider, build_provider
from minihermes.registry import discover_builtin_tools, registry
from minihermes.skills import SkillStore, default_skills_root

logger = logging.getLogger("minihermes.agent")

# 累计多少轮工具迭代后，提示模型考虑沉淀技能 / 写记忆。
# 对应 Hermes 的 _iters_since_skill / _turns_since_memory 计数阈值。
DEFAULT_NUDGE_AFTER = 10

_SKILL_NUDGE = (
    "[系统提示] 刚才这段任务用了不少步骤。如果你刚刚摸索出了一套**以后还会重复用到**的流程，"
    "现在是用 skill_manage(action='create') 把它沉淀下来的好时机。\n"
    "如果只是一次性任务、或者已有技能覆盖了，就忽略这条，不要为了建而建。"
)

_MEMORY_NUDGE = (
    "[系统提示] 如果刚才的对话里出现了关于用户、项目或环境的**稳定事实**"
    "（偏好、约定、路径、约束），可以用 memory_append 记下来。\n"
    "临时结果、一次性路径不要记 —— 记忆每轮都会注入，体积就是持续成本。"
)


def build_system_prompt(
    workspace: Path,
    skills: SkillStore,
    extra: str = "",
) -> str:
    """构建 system prompt。

    ⚠️ 这里的内容必须在会话期间**保持恒定**（技能清单变动除外）。
    任何每轮都可能变化的东西（记忆、检索结果）都不许写进来。
    """
    today = datetime.now().strftime("%Y-%m-%d %A")
    parts = [
        "你是 mini-hermes，一个精简的 AI Agent 运行内核。你可以调用工具来完成实际任务。",
        "",
        "## 环境",
        f"- 工作目录：{workspace}",
        f"- 当前日期：{today}",
        f"- 操作系统：{os.name}",
        "",
        "## 可用技能",
        "以下是已沉淀的技能清单。**只在确实相关时**用 skill_view 载入正文，不要一次读多个：",
        skills.catalog_block(),
        "",
        "## 工作方式",
        "- 需要了解现状时，先看再说，不要凭印象回答文件内容或目录结构。",
        "- 工具返回错误时，读懂错误再决定下一步；不要用同样的参数反复重试。",
        "- 完成任务后给一句简短结论，说清做了什么、结果如何。",
        "- 不要伪造工具调用结果。没调用工具就如实说没有。",
    ]
    if extra:
        parts += ["", extra]
    return "\n".join(parts)


class Agent:
    def __init__(
        self,
        provider: Provider | None = None,
        *,
        model: str = "",
        workspace: Path | str | None = None,
        skills_root: Path | str | None = None,
        memory_root: Path | str | None = None,
        max_iterations: int = 30,
        max_tokens: int = 32_000,
        callbacks: Callbacks | None = None,
        nudge_after: int = DEFAULT_NUDGE_AFTER,
        fallback_provider: Provider | None = None,
        extra_system: str = "",
        discover_tools: bool = True,
    ) -> None:
        # 顺序很关键：先定工作区，再做工具发现。
        # file/shell 工具在 import 时会读取 MINIHERMES_WORKSPACE，顺序反了它们会锚定 cwd。
        # 这里用直接赋值而不是 setdefault —— 显式传入的 workspace 必须能覆盖已有环境变量。
        self.workspace = Path(
            workspace or os.environ.get("MINIHERMES_WORKSPACE") or Path.cwd()
        ).resolve()
        os.environ["MINIHERMES_WORKSPACE"] = str(self.workspace)

        # 工具发现必须早于 system prompt 构建 —— 提示里要列可用工具。
        if discover_tools:
            discover_builtin_tools()

        self.provider = provider or build_provider(model or None)
        self.model = model
        self.callbacks = callbacks or null_callbacks()

        self.skills = SkillStore(Path(skills_root) if skills_root else default_skills_root())
        self.memory = MemoryStore(Path(memory_root) if memory_root else default_memory_root())

        # 把 store 注入给工具层，让模型调用的工具与 Agent 用同一份数据
        from minihermes.tools import memory_tool, skill_tools

        skill_tools.configure(self.skills)
        memory_tool.configure(self.memory)

        self.budget = IterationBudget(limit=max_iterations)
        self._extra_system = extra_system
        self.loop = ConversationLoop(
            provider=self.provider,
            system_prompt=build_system_prompt(self.workspace, self.skills, extra_system),
            compressor=ContextCompressor(max_tokens=max_tokens),
            budget=self.budget,
            callbacks=self.callbacks,
            context_injector=self._context_injector,
            fallback_provider=fallback_provider,
            model=model,
        )

        self.nudge_after = nudge_after
        self._iters_since_nudge = 0

    # ------------------------------------------------------------------ 上下文

    def _context_injector(self) -> str:
        """每轮调用，返回要拼进 user message 的动态上下文。

        只放动态内容。技能正文不在这里 —— 它由 skill_view 或 /skill-name 按需加载。
        """
        return self.memory.context_block()

    # ------------------------------------------------------------------ 交互

    def chat(self, text: str) -> TurnResult:
        """跑一个回合。支持 `/技能名` 形式直接载入技能。"""
        text = self._maybe_expand_skill(text)
        result = self.loop.run(text)
        self._maybe_nudge(result)
        return result

    def _maybe_expand_skill(self, text: str) -> str:
        """处理 `/skill-name [额外说明]`：把技能正文注入为 user message。"""
        if not text.startswith("/"):
            return text
        head, _, rest = text[1:].partition(" ")
        name = head.strip()
        if not name or not self.skills.exists(name):
            return text

        body = self.skills.get_body(name)
        # 技能正文走 user message，不走 system prompt —— 保前缀缓存。
        self.loop.inject(f"[已载入技能：{name}]\n\n{body}")
        return rest.strip() or f"请按照技能 {name} 的步骤执行。"

    # ------------------------------------------------------------------ 复盘

    def _maybe_nudge(self, result: TurnResult) -> None:
        """回合结束后按迭代计数触发软引导。

        触发是硬判定（计数到了必然发生），动作是软的（只是提示模型）。
        """
        if result.iterations <= 0:
            return
        self._iters_since_nudge += result.iterations
        if self._iters_since_nudge < self.nudge_after:
            return
        self._iters_since_nudge = 0
        self.loop.inject(_SKILL_NUDGE)
        # 技能提示与记忆提示交替给出，避免一次塞两条把模型带偏
        if self._nudge_toggle():
            self.loop.inject(_MEMORY_NUDGE)

    def _nudge_toggle(self) -> bool:
        self._toggle = not getattr(self, "_toggle", False)
        return self._toggle

    # ------------------------------------------------------------------ 控制

    def interrupt(self) -> None:
        self.loop.interrupt()

    def reset(self) -> None:
        self.loop.reset()
        self._iters_since_nudge = 0

    def refresh_system_prompt(self) -> None:
        """重建 system prompt（技能增删后调用）。

        ⚠️ 这是**有代价**的操作：system prompt 一变，前面所有轮次的前缀缓存全部作废。
        真实 Hermes 因此把技能清单的刷新做成**延迟失效**，需要显式 `/skills install --now`
        才立即生效 —— 与这里提供显式 refresh 是同一个思路：让"重建提示词"成为一个
        需要被意识到的动作，而不是每加一个技能就自动发生。
        """
        self.loop.system_prompt = build_system_prompt(
            self.workspace, self.skills, self._extra_system
        )

    # ------------------------------------------------------------------ 自省

    def describe(self) -> str:
        by_set = registry.tools_by_toolset()
        lines = [
            f"mini-hermes · model={self.model or '(mock)'}",
            f"工作目录：{self.workspace}",
            f"技能目录：{self.skills.root}",
            f"记忆目录：{self.memory.root}",
            f"预算：{self.budget.describe()}",
            "工具：",
        ]
        for toolset, names in sorted(by_set.items()):
            lines.append(f"  - {toolset}: {', '.join(names)}")
        return "\n".join(lines)
