"""
minihermes.registry —— 工具注册表

【架构地位】这是整个依赖链的**起点**，本模块不从包内任何其他模块 import 东西。
这条无环约束是从 Hermes Agent 蒸馏出来的第一条结构性规则：

    minihermes/registry.py        ← 无内部依赖（本文件）
              ^
    minihermes/tools/*.py         ← 在模块顶层调用 register()
              ^
    minihermes/loop.py, agent.py  ← 编排层

之所以要保证无环，是因为工具模块需要在 import 时就把自己注册进去（副作用注册），
如果注册表反过来依赖工具模块，就会形成循环导入死结。

【设计要点（对应白皮书 02-工具系统）】
  1. import 时副作用注册 + AST 自动发现 —— 新增工具只需放一个文件，不用改任何清单
  2. 同名冲突默认 **拒绝**，而不是悄悄覆盖 —— 防止后加载的工具顶掉内置工具
  3. 注册表任何变更都会 bump 一个单调递增的 generation，所有缓存按它判有效性
  4. check_fn 决定"这个工具现在能不能用"，带 TTL 缓存，不能每次问模型都重新探测
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger("minihermes.registry")

# check_fn 探测结果的缓存时长（秒）。可用性探测可能有 IO（查文件、ping 服务），
# 不能每轮对话都重跑；但也不能永久缓存，否则服务恢复了模型还用不了工具。
_CHECK_TTL_SECONDS = 30.0


@dataclass
class ToolEntry:
    """一个已注册的工具。"""

    name: str
    toolset: str
    schema: dict[str, Any]          # OpenAI function 体 {name, description, parameters}
    handler: Callable[[dict], str]  # 接 dict 参数，返回字符串结果
    check_fn: Callable[[], bool] | None = None
    description: str = ""
    emoji: str = ""
    max_result_chars: int = 40_000
    # 零参可调用，每次生成 schema 时浅合并进去。
    # 用途：把"当前实际可用的工具名列表"动态注入到描述里，
    # 而不是在 schema 里硬编码其它工具名（见白皮书：禁止硬编码跨工具引用）。
    dynamic_schema_overrides: Callable[[], dict] | None = None


class ToolRegistry:
    """进程级单例注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._lock = threading.RLock()
        # 单调递增。任何 mutation（register / deregister / MCP 刷新）都会 +1。
        # 缓存项记录自己是在哪个 generation 下算出来的，generation 变了就作废。
        self._generation = 0
        self._check_cache: dict[str, tuple[float, bool]] = {}
        self._discovered = False

    # ------------------------------------------------------------------ 属性

    @property
    def generation(self) -> int:
        return self._generation

    def _bump(self) -> None:
        self._generation += 1
        self._check_cache.clear()

    # ------------------------------------------------------------ 注册/注销

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[[dict], str],
        check_fn: Callable[[], bool] | None = None,
        description: str = "",
        emoji: str = "",
        max_result_chars: int = 40_000,
        dynamic_schema_overrides: Callable[[], dict] | None = None,
        override: bool = False,
    ) -> bool:
        """注册一个工具。返回是否成功。

        冲突策略（刻意保守）：
          * 同名 + 同 toolset            → 幂等，静默覆盖（模块被重复 import 时正常）
          * override=True                → 允许顶替，记审计日志
          * 其余一切                     → **拒绝**并记 error
        """
        with self._lock:
            existing = self._tools.get(name)

            if existing is not None:
                if existing.toolset == toolset:
                    pass  # 幂等重注册，放行
                elif override:
                    logger.info(
                        "工具被显式覆盖: %s (%s → %s)", name, existing.toolset, toolset
                    )
                else:
                    # 这是刻意为之的"默认拒绝"：扩展点越开放，冲突策略越要保守。
                    logger.error(
                        "工具注册被拒绝: %r 会遮蔽已有的 %s 工具（toolset=%s）。"
                        "如确需覆盖请显式传 override=True。",
                        name,
                        existing.toolset,
                        existing.toolset,
                    )
                    return False

            self._tools[name] = ToolEntry(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=check_fn,
                description=description,
                emoji=emoji,
                max_result_chars=max_result_chars,
                dynamic_schema_overrides=dynamic_schema_overrides,
            )
            self._bump()
            return True

    def deregister(self, name: str) -> bool:
        with self._lock:
            if name in self._tools:
                del self._tools[name]
                self._bump()
                return True
            return False

    # ------------------------------------------------------------- 可用性

    def is_available(self, name: str) -> bool:
        """工具当前是否可用（check_fn 通过与否）。带 TTL 缓存。"""
        entry = self._tools.get(name)
        if entry is None:
            return False
        if entry.check_fn is None:
            return True

        now = time.monotonic()
        cached = self._check_cache.get(name)
        if cached is not None:
            ts, ok = cached
            if now - ts < _CHECK_TTL_SECONDS:
                return ok

        try:
            ok = bool(entry.check_fn())
        except Exception:  # 探测抛出异常 → 视为不可用，绝不让它炸掉整个对话
            logger.warning("check_fn 对工具 %r 抛异常，按不可用处理", name, exc_info=True)
            ok = False

        self._check_cache[name] = (now, ok)
        return ok

    def refresh_availability(self) -> None:
        """清空可用性探测缓存，强制下次重新探测。

        为什么需要这个：check_fn 结果有 TTL 缓存（默认 30 秒），这意味着
        外部状态变化后**最多有 30 秒的滞后** —— 服务刚恢复、密钥刚配好时，
        模型可能还会看到"工具不可用"。这是刻意的权衡（避免每轮都做 IO 探测），
        但调用方在已知状态变化时可以主动调这个方法来消除滞后。
        """
        with self._lock:
            self._check_cache.clear()

    # ------------------------------------------------------------- schema

    def get_definitions(
        self, toolsets: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """返回给模型的工具定义列表（OpenAI tools 格式）。

        只包含 check_fn 通过的工具 —— 模型看不见用不了的工具，
        因此也就不会去幻觉调用它们。
        """
        allowed = set(toolsets) if toolsets is not None else None
        out: list[dict[str, Any]] = []
        for name, entry in sorted(self._tools.items()):
            if allowed is not None and entry.toolset not in allowed:
                continue
            if not self.is_available(name):
                continue

            schema = _deepcopy_json(entry.schema)
            if entry.dynamic_schema_overrides is not None:
                try:
                    schema.update(entry.dynamic_schema_overrides() or {})
                except Exception:
                    logger.warning("dynamic_schema_overrides 失败: %s", name, exc_info=True)
            schema.setdefault("name", name)
            out.append({"type": "function", "function": schema})
        return out

    def available_names(self) -> list[str]:
        """当前真正可用的工具名。用于动态交叉引用注入。"""
        return [n for n in sorted(self._tools) if self.is_available(n)]

    # ------------------------------------------------------------- 分发

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        """执行工具，返回字符串结果。异常会被包装成可读文本交给模型。"""
        entry = self._tools.get(name)
        if entry is None:
            return f"错误：不存在名为 {name!r} 的工具。"
        if not self.is_available(name):
            return f"错误：工具 {name!r} 当前不可用（前置条件未满足）。"
        try:
            result = entry.handler(args or {})
        except Exception as exc:
            logger.warning("工具 %s 执行失败", name, exc_info=True)
            return f"错误：工具 {name!r} 执行失败 —— {type(exc).__name__}: {exc}"

        if not isinstance(result, str):
            result = str(result)
        # 三层截断里的第一层：单工具结果上限
        if len(result) > entry.max_result_chars:
            keep = entry.max_result_chars
            result = (
                result[:keep]
                + f"\n\n…[结果被截断：原始长度 {len(result)} 字符，"
                f"上限 {keep}。请用更精确的参数重试。]"
            )
        return result

    # ------------------------------------------------------------- 发现

    # ------------------------------------------------------------- 查询

    def toolset_of(self, name: str) -> str:
        """工具归属的 toolset；不存在则返回空串。"""
        entry = self._tools.get(name)
        return entry.toolset if entry else ""

    def tools_by_toolset(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for name, entry in self._tools.items():
            out.setdefault(entry.toolset, []).append(name)
        for v in out.values():
            v.sort()
        return out


registry = ToolRegistry()


def register(**kwargs: Any) -> bool:
    """模块级快捷注册。工具文件在顶层这样写：

        register(name="read_file", toolset="file", schema={...}, handler=_read_file)
    """
    return registry.register(**kwargs)


# --------------------------------------------------------------------- 自动发现

def discover_builtin_tools(package_dir: Path | None = None) -> int:
    """扫描 tools/ 目录下的 .py，导入其中调用了 register(...) 的模块。

    为什么用 **AST 静态扫描** 而不是维护一个文件名白名单：
    白名单会腐化 —— 有人加了新工具文件忘了登记，工具就静默失效了。
    AST 扫描让"文件存在"就等于"已注册"，新增工具零成本。

    返回成功导入的模块数。
    """
    if package_dir is None:
        package_dir = Path(__file__).parent / "tools"

    if registry._discovered:
        return 0

    imported = 0
    for path in sorted(package_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        if not _calls_register(path):
            continue
        mod_name = f"minihermes.tools.{path.stem}"
        try:
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])
            else:
                spec = importlib.util.spec_from_file_location(mod_name, path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
            imported += 1
        except Exception:
            logger.exception("导入工具模块失败: %s", path.name)
    registry._discovered = True
    return imported


def _calls_register(path: Path) -> bool:
    """AST 扫描：这个文件顶层有没有调用 register(...)。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        logger.warning("无法解析工具文件: %s", path.name)
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "register":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "register":
                return True
    return False


def _deepcopy_json(obj: Any) -> Any:
    """不引入 copy 模块的浅层 JSON 深拷贝（schema 都是纯 JSON 结构）。"""
    import json

    return json.loads(json.dumps(obj))
