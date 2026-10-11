"""
文件操作工具集。

【架构】本模块在 import 时顶层调用 register()，把自己注册进注册表。
新增一个工具文件，不需要修改任何清单 —— registry.discover_builtin_tools()
会用 AST 扫描发现它。

【依赖方向】tools/* → registry（合法）；registry 永远不反向依赖 tools。
"""

from __future__ import annotations

import os
from pathlib import Path

from minihermes.registry import register

def _workspace() -> Path:
    """工作根目录。所有文件操作都被限制在这个目录内（对应白皮书的 path_security）。

    ⚠️ 必须是**函数**，不能写成模块级常量：
    Agent 构造时先 discover 工具、随后才设定工作区，而模块级常量在 import 那一刻
    就被求值并永久冻结成 cwd —— 之后再改也无效（真实模型验证时踩过这个坑，
    write_file 把文件落到了项目目录而不是沙箱）。
    """
    return Path(os.environ.get("MINIHERMES_WORKSPACE") or Path.cwd()).resolve()


_MAX_READ_BYTES = 200_000


def _safe_path(raw: str) -> Path:
    """把用户给的路径解析成绝对路径，并确保它没跑出工作区。

    这是"路径安全"最小实现：符号链接、.. 穿越都在 resolve() 阶段被归一化，
    之后做一次前缀比较。真实系统还要处理 TOCTOU 与 symlink 竞态。
    """
    if not raw or not raw.strip():
        raise ValueError("路径不能为空")
    ws = _workspace()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = ws / candidate
    resolved = candidate.resolve()
    if resolved != ws and ws not in resolved.parents:
        raise PermissionError(f"路径越界，拒绝访问工作区之外的位置: {resolved}")
    return resolved


# --------------------------------------------------------------------- 工具实现

def _read_file(args: dict) -> str:
    path = _safe_path(str(args.get("path", "")))
    if not path.exists():
        return f"错误：文件不存在 —— {path}"
    if path.is_dir():
        return f"错误：{path} 是目录，请用 list_dir。"

    size = path.stat().st_size
    if size > _MAX_READ_BYTES:
        return (
            f"错误：文件过大（{size} 字节 > {_MAX_READ_BYTES} 上限）。"
            "请改用 list_dir 或分块读取。"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"错误：{path} 不是 UTF-8 文本文件，无法读取。"

    lines = text.splitlines()
    return f"# {path}（共 {len(lines)} 行）\n" + text


def _write_file(args: dict) -> str:
    path = _safe_path(str(args.get("path", "")))
    content = args.get("content")
    if content is None:
        return "错误：缺少 content 参数。"
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(str(content), encoding="utf-8")
    verb = "覆盖写入" if existed else "新建"
    return f"已{verb} {path}（{len(str(content))} 字符）"


def _list_dir(args: dict) -> str:
    path = _safe_path(str(args.get("path", ".")))
    if not path.exists():
        return f"错误：目录不存在 —— {path}"
    if not path.is_dir():
        return f"错误：{path} 是文件，请用 read_file。"

    entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
    if not entries:
        return f"# {path}（空目录）"
    lines = [f"# {path}"]
    for e in entries:
        if e.is_dir():
            lines.append(f"  [DIR ] {e.name}/")
        else:
            lines.append(f"  [FILE] {e.name}  ({e.stat().st_size} B)")
    return "\n".join(lines)


# --------------------------------------------------------------------- 注册

register(
    name="read_file",
    toolset="file",
    description="读取工作区内的 UTF-8 文本文件",
    emoji="📄",
    schema={
        "name": "read_file",
        "description": "读取工作区内的一个文本文件并返回其内容。路径可以是相对工作区的相对路径。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（相对于工作区或绝对路径）"}
            },
            "required": ["path"],
        },
    },
    handler=_read_file,
)

register(
    name="write_file",
    toolset="file",
    description="在工作区内写入文本文件（不存在则新建，存在则覆盖）",
    emoji="💾",
    schema={
        "name": "write_file",
        "description": (
            "把文本内容写入工作区内的文件。父目录会自动创建。已存在的文件会被整体覆盖。"
            "注意：本工具不做局部编辑，需要改一处请先 read_file 再整体 write_file。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目标文件路径"},
                "content": {"type": "string", "description": "要写入的完整文本内容"},
            },
            "required": ["path", "content"],
        },
    },
    handler=_write_file,
)

register(
    name="list_dir",
    toolset="file",
    description="列出工作区内某个目录的内容",
    emoji="📁",
    schema={
        "name": "list_dir",
        "description": "列出目录下的文件与子目录，附带文件大小。用于在读写之前了解目录结构。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径，默认当前工作区根目录",
                }
            },
            "required": [],
        },
    },
    handler=_list_dir,
)
