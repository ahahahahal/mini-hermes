"""
minihermes.callbacks —— 事件回调

【架构】对应白皮书铁律四「内核与呈现彻底解耦」。

Hermes 的 AIAgent 有 11 个回调，内核几乎不直接 print 任何用户可见内容。
这里保留其中最有用的几个，原则不变：

    **内核负责"发生了什么"，调用方决定"怎么显示"。**

所以同一个内核可以驱动交互式 CLI、单次批处理脚本、或者根本不显示任何东西的
自动化流水线，不需要改内核一行代码。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("minihermes.callbacks")


@dataclass
class Callbacks:
    on_status: Optional[Callable[[str], None]] = None
    """粗粒度状态文本，如"思考中…"、"压缩上下文…"。"""

    on_tool_start: Optional[Callable[[str, dict], None]] = None
    """工具即将执行：(工具名, 参数)。"""

    on_tool_end: Optional[Callable[[str, str], None]] = None
    """工具执行完毕：(工具名, 结果文本)。"""

    on_assistant_text: Optional[Callable[[str], None]] = None
    """模型产出可见文本。"""

    on_error: Optional[Callable[[BaseException, str], None]] = None
    """出错：(异常, 人类可读说明)。"""

    on_budget: Optional[Callable[[Any], None]] = None
    """预算变化时回调，收到 IterationBudget 对象。"""

    _safe: list[str] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------

    def emit(self, name: str, *args: Any) -> None:
        """安全触发。**回调抛异常绝不能炸掉内核** —— 显示层出问题不该让对话中断。"""
        fn = getattr(self, name, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            logger.warning("回调 %s 抛出异常，已忽略", name, exc_info=True)
            self._safe.append(name)


def null_callbacks() -> Callbacks:
    """什么都不做的回调集合，用于测试与批处理。"""
    return Callbacks()
