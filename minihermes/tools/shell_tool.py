"""
Shell 执行工具。

【架构】对应白皮书 02-工具系统 §8「安全与审批」。
这里实现最小但真实的两级门禁：

  * hardline  —— 无论如何都不执行，连"放开一切"的开关都绕不过（地板）
  * dangerous —— 默认拒绝，必须显式设置 MINIHERMES_ALLOW_DANGEROUS=1 才放行

真实 Hermes 的 hardline 之上还有 tirith 内容扫描、sudo stdin 拦截、SSRF 防护等，
这里只保留最核心的两级，作为"安全是有地板"的示范。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from minihermes.registry import register

def _workspace() -> Path:
    """执行 shell 时的工作目录。

    ⚠️ 函数而非模块级常量 —— 理由同 file_tools._workspace()：常量会在 import
    时被冻结，导致 Agent 传入的 workspace 失效。
    """
    return Path(os.environ.get("MINIHERMES_WORKSPACE") or Path.cwd()).resolve()


_TIMEOUT_SECONDS = 60

# 地板：这些模式永远拒绝执行。不是"需要确认"，是"没有这个选项"。
_HARDLINE = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*r[a-zA-Z]*f\s+/(\s|$)"), "递归删除根目录"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "格式化文件系统"),
    (re.compile(r"\bdd\b.*\bof=/dev/(sd|nvme|disk)"), "裸写块设备"),
    (re.compile(r":\(\)\s*\{.*\}\s*;\s*:"), "fork 炸弹"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "关机/重启"),
    (re.compile(r"\bchmod\s+-R\s+777\s+/(\s|$)"), "对根目录开放全部权限"),
]

# 危险但可授权：默认拒绝。
_DANGEROUS = [
    (re.compile(r"\brm\b"), "删除操作"),
    (re.compile(r"\bgit\s+push\b.*--force"), "强推远程分支"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "丢弃本地改动"),
    (re.compile(r"\|\s*(sudo\s+)?(ba)?sh\b"), "管道执行远程脚本"),
    (re.compile(r"\bcurl\b.*\|\s*"), "管道执行远程脚本"),
    (re.compile(r"\bsudo\b"), "提权执行"),
]


def _allow_dangerous() -> bool:
    return os.environ.get("MINIHERMES_ALLOW_DANGEROUS", "").strip() in {"1", "true", "yes"}


def _check_command(cmd: str) -> str | None:
    """返回拒绝理由；None 表示放行。这是唯一的安全决策点。"""
    for pattern, why in _HARDLINE:
        if pattern.search(cmd):
            return f"⛔ 已被安全地板拦截（不可绕过）：{why}。这是硬编码的禁止项，没有开关可以放行。"
    for pattern, why in _DANGEROUS:
        if pattern.search(cmd):
            if _allow_dangerous():
                return None
            return (
                f"⚠️ 危险操作暂缓：{why}。默认拒绝执行。"
                f"如确需执行，请在启动时设置环境变量 MINIHERMES_ALLOW_DANGEROUS=1。"
            )
    return None


def _run_shell(args: dict) -> str:
    cmd = str(args.get("command", "")).strip()
    if not cmd:
        return "错误：command 不能为空。"

    reason = _check_command(cmd)
    if reason:
        return reason

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(_workspace()),
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"错误：命令执行超过 {_TIMEOUT_SECONDS} 秒已被终止（命令：{cmd}）。"
    except Exception as exc:
        return f"错误：无法执行命令 —— {type(exc).__name__}: {exc}"

    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()

    parts = [f"$ {cmd}", f"exit_code={proc.returncode}"]
    if stdout:
        parts.append(f"--- stdout ---\n{stdout}")
    if stderr:
        parts.append(f"--- stderr ---\n{stderr}")
    if not stdout and not stderr:
        parts.append("(无输出)")
    return "\n".join(parts)


register(
    name="run_shell",
    toolset="shell",
    description="在工作区执行 shell 命令（带回显、超时与安全门禁）",
    emoji="🖥️",
    max_result_chars=20_000,
    schema={
        "name": "run_shell",
        "description": (
            "在工作区目录下执行一条 shell 命令并返回 stdout/stderr 与退出码。"
            f"超时 {_TIMEOUT_SECONDS} 秒。注意：删除类、提权类、管道执行远程脚本类操作默认被拒绝，"
            "会被拦截并返回说明；不要反复重试同一个被拒命令。"
            f"当前平台：{sys.platform}。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的完整 shell 命令"}
            },
            "required": ["command"],
        },
    },
    handler=_run_shell,
)
