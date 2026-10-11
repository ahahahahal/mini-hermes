"""
mini-hermes —— 从 Nous Research 的 Hermes Agent 蒸馏出来的可运行最小内核。

它保留了 Hermes 的架构骨架与核心约束：

  * 无环依赖链：registry ← tools ← loop
  * import 时副作用注册 + AST 自动发现（新增工具零改动）
  * 同名工具冲突默认拒绝
  * check_fn 可用性探测 + TTL 缓存
  * schema 描述不硬编码跨工具引用（动态注入可用工具名）
  * prompt 缓存铁律：技能正文与记忆一律注入 user message，不进 system prompt
  * 技能清单进 system prompt，正文按需加载
  * 迭代预算（execute_code 类廉价调用退还预算）
  * 上下文压缩：唯一允许改写历史的时刻
  * 错误分类 → 重试 → 降级链
  * 安全地板：hardline 命令连"放开一切"的开关都绕不过
  * 事件回调：内核不直接 print，呈现层完全外置

它**没有**保留（有意简化，见 README「未保留的部分」）：
多平台网关、TUI/ACP、插件热加载、MCP、子 agent 委派、8 种记忆后端、
FTS5 会话检索、向量检索、轨迹压缩、沙箱后端。

那些属于"外围极大"的部分；这个内核只回答一个问题：
**Hermes 的骨架到底是什么，能不能用几千行跑起来。**
"""

from minihermes.agent import Agent
from minihermes.provider import MockProvider, OpenAICompatProvider
from minihermes.registry import discover_builtin_tools, registry

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "MockProvider",
    "OpenAICompatProvider",
    "discover_builtin_tools",
    "registry",
    "__version__",
]
