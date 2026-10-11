"""
minihermes.budget —— 迭代预算

【架构】对应白皮书 01-Agent内核 §4。

三条从 Hermes 抄来的、容易被忽略的规则：

1. **不给模型中途施压**。预算耗尽前**不**告诉模型"你快没预算了"。
   Hermes 的源码注释里写得很清楚：中途施加压力会让模型在复杂任务上
   **过早放弃**（issue #7915）。压力只在真正耗尽时施加一次，并给最后一次机会。

2. **有退还机制**。程序化的廉价调用（比如用代码批量调工具）不应该消耗迭代预算，
   否则一个本该 1 轮的批量操作会被当成 30 轮。

3. **父子共享**。子任务消耗的额度要从父的额度里扣，否则派发几个子 agent
   就能把总预算翻几倍。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IterationBudget:
    limit: int = 90
    _used: int = 0
    _parent: "IterationBudget | None" = field(default=None, repr=False)

    # -------------------------------------------------------------- 查询

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        return max(self.limit - self._used, 0)

    @property
    def exhausted(self) -> bool:
        return self._used >= self.limit

    # -------------------------------------------------------------- 变更

    def consume(self, n: int = 1) -> None:
        """消耗额度。子预算的消耗会同步记到父预算上。"""
        self._used += n
        if self._parent is not None:
            # 父预算的 used 只作为"总账"记录，不再向更上层同步
            if self._parent._parent is None:
                self._parent._used += n
            else:
                self._parent.consume(n)

    def refund(self, n: int = 1) -> None:
        """退还额度。用于"这轮只调了廉价工具，不算数"的场景。"""
        self._used = max(self._used - n, 0)
        if self._parent is not None:
            self._parent.refund(n)

    # -------------------------------------------------------------- 派生

    def child(self, limit: int | None = None) -> "IterationBudget":
        """派生子预算。默认给 1/4 的剩余额度，且计入父账。"""
        child_limit = limit if limit is not None else max(self.remaining // 4, 5)
        return IterationBudget(limit=child_limit, _parent=self)

    def describe(self) -> str:
        return f"{self.used}/{self.limit}（剩余 {self.remaining}）"


# 这些工具被视作"廉价 RPC"，单独调用时不消耗迭代预算。
# 对应 Hermes: conversation_loop.py 里的 budget refund 分支。
CHEAP_TOOLSETS = {"skills", "memory"}
