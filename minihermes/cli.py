"""
minihermes.cli —— 交互式命令行前端

【架构】对应白皮书的"前端层"。它的存在意义是证明一件事：
**换一个前端不需要改内核**。这里所有输出都通过 Callbacks 拿到，
内核本身完全不知道"终端"的存在。
"""

from __future__ import annotations

import sys
from pathlib import Path

from minihermes.agent import Agent
from minihermes.callbacks import Callbacks

BANNER = """\
╭──────────────────────────────────────────────╮
│  mini-hermes —— Hermes Agent 的最小可运行内核  │
╰──────────────────────────────────────────────╯
  输入 /help 看命令；/exit 退出
"""

HELP = """\
可用命令：
  /help              显示本帮助
  /tools             列出已注册工具
  /skills            列出技能
  /skill <名字>       载入技能
  /memory            查看记忆
  /history           查看对话历史条数
  /reset             清空对话历史
  /exit              退出
其余输入直接交给 agent。
"""


def _force_utf8() -> None:
    """Windows 控制台默认可能是 GBK，中文输出会乱码或抛异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def make_callbacks(verbose: bool = True) -> Callbacks:
    def on_tool_start(name: str, args: dict) -> None:
        if not verbose:
            return
        shown = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
        print(f"  ⚙  {name}({shown})")

    def on_tool_end(name: str, result: str) -> None:
        if not verbose:
            return
        first = result.strip().splitlines()[0] if result.strip() else "(空)"
        print(f"     ↳ {_short(first, 100)}")

    def on_status(msg: str) -> None:
        if verbose:
            print(f"  … {msg}")

    def on_error(exc: BaseException, msg: str) -> None:
        print(f"  ⚠ {msg}")

    return Callbacks(
        on_tool_start=on_tool_start,
        on_tool_end=on_tool_end,
        on_status=on_status,
        on_error=on_error,
    )


def _short(value, limit: int = 60) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)
    workspace = Path(argv[0]).resolve() if argv else Path.cwd()

    agent = Agent(workspace=workspace, callbacks=make_callbacks())
    print(BANNER)
    print(agent.describe())
    print()

    while True:
        try:
            line = input("you ▸ ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        if line.startswith("/"):
            if _handle_command(agent, line):
                break
            continue

        result = agent.chat(line)
        if result.text:
            print(f"\nagent ▸ {result.text}\n")
        if result.stop_reason != "completed":
            print(f"  (停止原因：{result.stop_reason}，迭代 {result.iterations})\n")

    return 0


def _handle_command(agent: Agent, line: str) -> bool:
    """处理斜杠命令。返回 True 表示要退出。"""
    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/exit", "/quit", "/q"):
        return True
    if cmd == "/help":
        print(HELP)
    elif cmd == "/tools":
        print(agent.describe())
    elif cmd == "/skills":
        metas = agent.skills.list_meta()
        if not metas:
            print("  (还没有技能)")
        for m in metas:
            print(f"  - {m.name}: {m.description}")
    elif cmd == "/skill":
        if not arg:
            print("  用法：/skill <名字>")
        elif not agent.skills.exists(arg):
            print(f"  没有名为 {arg!r} 的技能")
        else:
            body = agent.skills.get_body(arg)
            print(f"\n--- {arg} ---\n{body}\n-----------\n")
    elif cmd == "/memory":
        block = agent.memory.context_block()
        print(block or "  (记忆为空)")
    elif cmd == "/history":
        print(f"  历史消息数：{len(agent.loop.history)}")
    elif cmd == "/reset":
        agent.reset()
        print("  已清空对话历史")
    else:
        print(f"  未知命令 {cmd}，输入 /help 查看帮助")
    return False


if __name__ == "__main__":
    raise SystemExit(main())
