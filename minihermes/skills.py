"""
minihermes.skills —— 技能系统（程序性记忆）

【架构地位】这是 Hermes Agent 最独特的部分，也是"自我改进闭环"的物理载体之一。

【必须记住的核心铁律】
    技能**正文**按需加载，并且作为 **user message** 注入，**绝不进 system prompt**。
    system prompt 里只放一份"技能清单"（name + description）。

    原因：system prompt 一旦变化，前缀缓存（prefix cache）全部失效，成本暴涨。
    技能是随时会新增/修改的东西，把它放进 system prompt 等于每加一个技能就让
    整个会话的缓存作废。所以：

        system prompt  ← 恒定的技能**清单**（只在技能增删时才需要重建）
        user message   ← /skill-name 或 skill_view 时才载入的技能**正文**

【技能文件格式】（沿用 agentskills.io 开放标准）

    skills/
    └── my-skill/
        ├── SKILL.md          # 必需：YAML frontmatter + 正文
        ├── references/       # 支撑文档
        ├── templates/        # 输出模板
        └── scripts/          # 可复跑脚本

【依赖方向】skills.py 只依赖标准库 —— 它被 tools/skill_tools.py 调用，
            但自己不反向依赖工具层。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

SKILL_FILENAME = "SKILL.md"
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def default_skills_root() -> Path:
    return Path(os.environ.get("MINIHERMES_HOME", Path.home() / ".minihermes")) / "skills"


@dataclass
class SkillMeta:
    name: str
    description: str
    path: Path
    version: str = ""
    tags: list[str] = field(default_factory=list)


class SkillStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_skills_root()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 扫描

    def _skill_files(self):
        """技能可以平铺，也可以按分类嵌套一层。"""
        yield from sorted(self.root.glob(f"*/{SKILL_FILENAME}"))
        yield from sorted(self.root.glob(f"*/*/{SKILL_FILENAME}"))

    def list_meta(self) -> list[SkillMeta]:
        metas: list[SkillMeta] = []
        for path in self._skill_files():
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            fm, _ = parse_frontmatter(raw)
            name = str(fm.get("name") or path.parent.name).strip()
            metas.append(
                SkillMeta(
                    name=name,
                    description=str(fm.get("description", "")).strip(),
                    path=path,
                    version=str(fm.get("version", "")).strip(),
                    tags=_parse_tags(fm.get("tags")),
                )
            )
        return sorted(metas, key=lambda m: m.name)

    def exists(self, name: str) -> bool:
        return self._find(name) is not None

    def _find(self, name: str) -> Path | None:
        for path in self._skill_files():
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            if str(fm.get("name") or path.parent.name).strip() == name:
                return path
        return None

    # ------------------------------------------------------------------ 读取

    def get_body(self, name: str) -> str:
        """取技能正文（不含 frontmatter）—— 这个字符串会被塞进 user message。"""
        path = self._find(name)
        if path is None:
            return f"错误：不存在名为 {name!r} 的技能。"
        _, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        return body.strip()

    def catalog_block(self) -> str:
        """system prompt 里那份"技能清单"。只有元数据，没有正文。"""
        metas = self.list_meta()
        if not metas:
            return "(当前没有已安装的技能)"
        lines = []
        for m in metas:
            desc = m.description or "(无描述)"
            lines.append(f"- {m.name}: {desc}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 写入

    def create(self, name: str, description: str, body: str) -> str:
        if not _NAME_RE.match(name):
            return (
                f"错误：技能名 {name!r} 非法。只允许小写字母、数字与连字符，"
                "且必须以字母或数字开头，最长 64 字符。"
            )
        if not description.strip():
            return "错误：description 必填 —— 它是模型唯一能看到的元数据，决定技能会不会被用上。"
        if self.exists(name):
            return f"错误：技能 {name!r} 已存在。修改请用 patch_skill。"

        path = self.root / name / SKILL_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        frontmatter = f"---\nname: {name}\ndescription: {description.strip()}\n"
        frontmatter += f"version: 1\nmetadata:\n  created_by: agent\n---\n\n"
        path.write_text(frontmatter + body.strip() + "\n", encoding="utf-8")
        return f"✅ 已创建技能 {name!r} → {path}"

    def patch(self, name: str, old: str, new: str) -> str:
        """局部替换技能正文。

        与文件编辑一样，先做空白归一化再匹配 —— 模型给出的 old 片段经常
        在缩进/行尾空白上与文件对不上，严格匹配会让它反复失败。
        """
        path = self._find(name)
        if path is None:
            return f"错误：不存在名为 {name!r} 的技能。"
        raw = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)
        if old not in body:
            loose = _normalize(body)
            if _normalize(old) not in loose:
                return (
                    f"错误：在技能 {name!r} 中找不到待替换片段。"
                    "请先用 skill_view 读取当前正文，复制一段完全一致的原文再试。"
                )
            body = loose.replace(_normalize(old), new, 1)
        else:
            body = body.replace(old, new, 1)

        # 重新拼装：frontmatter 原样保留，只替换正文部分
        parts = raw.split("---", 2)
        head = f"---{parts[1]}---" if len(parts) == 3 else ""
        path.write_text(f"{head}\n\n{body.strip()}\n", encoding="utf-8")
        return f"✅ 已更新技能 {name!r}（{path}）"


# --------------------------------------------------------------------- 解析工具

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 `---` 包裹的 YAML 子集，返回 (元数据, 正文)。

    只支持 `key: value` 与 `tags: [a, b]` 这类扁平写法 —— 不引入 yaml 依赖。
    """
    if not text.startswith("---"):
        return {}, text
    end = re.search(r"\n---\s*\n", text[3:])
    if not end:
        return {}, text
    block = text[3 : end.start() + 3]
    body = text[end.end() + 3 :]
    meta: dict = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body


def _parse_tags(value) -> list[str]:
    if not value:
        return []
    text = str(value).strip().strip("[]")
    return [t.strip().strip("'\"") for t in text.split(",") if t.strip()]


def _normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())
