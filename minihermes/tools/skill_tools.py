"""
技能相关工具。

【架构】对应白皮书 03-自进化闭环 §3。这里是"自我改进闭环"里
模型能主动调用的那一半（另一半是回合结束时的自动复盘，见 loop.py）。

技能**清单**常驻 system prompt（只是 name + description），
技能**正文**永远按需加载 —— 这条区分是整个自进化机制不拖垮缓存成本的关键。
"""

from __future__ import annotations

from minihermes.registry import register
from minihermes.skills import SkillStore, default_skills_root

_store: SkillStore | None = None


def configure(store: SkillStore) -> None:
    """由 agent 注入自定义 SkillStore（否则用默认 HOME 下的）。"""
    global _store
    _store = store


def get_store() -> SkillStore:
    global _store
    if _store is None:
        _store = SkillStore(default_skills_root())
    return _store


# --------------------------------------------------------------------- 实现

def _skills_list(args: dict) -> str:
    metas = get_store().list_meta()
    if not metas:
        return "当前没有任何技能。可以先用 skill_manage 创建一个。"
    lines = [f"共 {len(metas)} 个技能："]
    for m in metas:
        tag = f" [{', '.join(m.tags)}]" if m.tags else ""
        lines.append(f"- {m.name}{tag}: {m.description or '(无描述)'}")
    return "\n".join(lines)


def _skill_view(args: dict) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return "错误：缺少 name 参数。"
    body = get_store().get_body(name)
    if body.startswith("错误："):
        return body
    return f"# 技能：{name}\n\n{body}"


def _skill_manage(args: dict) -> str:
    action = str(args.get("action", "")).strip()
    store = get_store()

    if action == "create":
        name = str(args.get("name", "")).strip()
        description = str(args.get("description", "")).strip()
        body = str(args.get("body", "")).strip()
        if not body:
            return "错误：body 不能为空 —— 技能正文才是真正有用的部分。"
        return store.create(name, description, body)

    if action == "patch":
        name = str(args.get("name", "")).strip()
        old = str(args.get("old", ""))
        new = str(args.get("new", ""))
        if not name or not old:
            return "错误：patch 需要 name 与 old 参数。"
        return store.patch(name, old, new)

    return f"错误：未知 action {action!r}。可选：create、patch。"


# --------------------------------------------------------------------- 注册

register(
    name="skills_list",
    toolset="skills",
    description="列出当前已安装的技能",
    emoji="📚",
    schema={
        "name": "skills_list",
        "description": "列出所有已安装技能的名称与描述。开始一个可能已有现成流程的任务前，先看这里。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    handler=_skills_list,
)

register(
    name="skill_view",
    toolset="skills",
    description="读取某个技能的完整正文",
    emoji="📖",
    schema={
        "name": "skill_view",
        "description": (
            "读取指定技能的完整正文与步骤。当你确定某个技能与当前任务相关时，"
            "用这个工具把它载入。只载入真正需要的技能，不要一次性读多个。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "技能名"}},
            "required": ["name"],
        },
    },
    handler=_skill_view,
)

register(
    name="skill_manage",
    toolset="skills",
    description="创建新技能，或修改已有技能的正文",
    emoji="🛠️",
    schema={
        "name": "skill_manage",
        "description": (
            "管理技能。action='create' 新建一个技能（需要 name/description/body）；"
            "action='patch' 局部修改已有技能正文（需要 name/old/new）。\n"
            "什么时候该建技能：当你完成了一个**多步骤、以后可能重复遇到**的任务，"
            "并且这套流程不是从现有技能里抄来的 —— 把可复用的流程沉淀成技能。\n"
            "什么时候不该建：一次性的临时任务、任务太简单、或者现有技能已经覆盖了。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "patch"]},
                "name": {
                    "type": "string",
                    "description": "技能名，小写字母/数字/连字符",
                },
                "description": {
                    "type": "string",
                    "description": "一句话说明这个技能解决什么问题（create 必填）",
                },
                "body": {
                    "type": "string",
                    "description": "技能正文，Markdown，写清步骤与坑（create 必填）",
                },
                "old": {"type": "string", "description": "patch：待替换的原文片段"},
                "new": {"type": "string", "description": "patch：替换后的新内容"},
            },
            "required": ["action"],
        },
    },
    handler=_skill_manage,
)
