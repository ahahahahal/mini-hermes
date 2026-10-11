# mini-hermes

从 [Nous Research 的 Hermes Agent](https://github.com/NousResearch/hermes-agent)（10 万行 Python）
**蒸馏出来的可运行最小内核**。

它不追求功能覆盖，只回答一个问题：

> **Hermes 的架构骨架到底是什么，能不能用几千行跑起来？**

答案：**能**。而且不需要装任何第三方包 —— 全部是 Python 标准库。

---

## 快速开始

```bash
# 1. 端到端演示（不联网、不需要 API key，8 项机制逐条实际执行）
python run_demo.py

# 2. 测试套件（44 个用例）
python -m unittest discover -s tests -v

# 3. 交互式 CLI（默认用 mock 模型，能跑但没有智能）
python -m minihermes.cli

# 4. 真实模型验证（默认从原版 Hermes 的 .env 借 key；也可自设环境变量）
python verify_real_llm.py
```

### 接真实模型

任何 OpenAI 兼容端点都能用（OpenRouter / DeepSeek / Moonshot / 本地 vLLM / ollama…）：

```bash
export MINIHERMES_API_KEY=sk-xxxxxx
export MINIHERMES_BASE_URL=https://api.deepseek.com/v1
export MINIHERMES_MODEL=deepseek-chat
python -m minihermes.cli
```

没配 `MINIHERMES_API_KEY` 时自动落到 `MockProvider` —— 保证内核**任何时候都能跑起来**。

> **实测状态（2026-09-29）**：已用 DeepSeek 真实端点跑通 3 轮对话 + 2 次真实工具调用，
> 模型自行决定调用 `write_file` / `read_file` 并成功落盘。复现：
> `python verify_real_llm.py`（会借用 `G:\Hermes\.hermes\.env` 里的 key，不打印明文）。

---

## 架构

### 依赖链（无环）

```
minihermes/registry.py          ← 零依赖，链条起点
          ↑
minihermes/tools/*.py           ← 模块顶层调用 register()（import 副作用注册）
          ↑
minihermes/loop.py / agent.py   ← 编排层
```

这条无环约束是**第一条**从 Hermes 蒸馏出来的结构性规则：工具要在 import 时就自注册，
所以注册表绝不能反向依赖工具模块，否则循环导入死结。

### 文件地图

| 文件 | 职责 | 对应白皮书章节 |
|---|---|---|
| `registry.py` | 工具注册表、AST 自动发现、冲突拒绝、check_fn 缓存 | 02 §1-§3 |
| `tools/file_tools.py` | read/write/list + 路径安全 | 02 §8.6 |
| `tools/shell_tool.py` | shell 执行 + hardline 安全地板 | 02 §8.1-§8.5 |
| `tools/skill_tools.py` | skills_list / skill_view / skill_manage | 03 §3-§4 |
| `tools/memory_tool.py` | memory 读写 | 03 §6.4 |
| `loop.py` | **主循环**：中断 / 重试 / 压缩 / 预算 / 工具分发 | 01 §2-§4 |
| `context.py` | 上下文压缩（按回合切分，保配对） | 01 §6 |
| `budget.py` | 迭代预算、退还、父子共享 | 01 §4 |
| `errors.py` | 错误分类 → 重试 / 压缩 / 降级 | 01 §7 |
| `provider.py` | OpenAI 兼容后端 + Mock 后端 | 04 §11 |
| `skills.py` | SKILL.md 扫描、frontmatter 解析、按需加载 | 03 §2 |
| `memory.py` | MEMORY.md / USER.md 读写与体积上限 | 03 §6 |
| `callbacks.py` | 事件回调（内核不 print） | 铁律四 |
| `agent.py` | 组装、system prompt 构建、软引导复盘 | 03 §3.1 |
| `cli.py` | 交互式前端 | 04 §7 |

---

## 保留了哪些 Hermes 机制

每一条都在 `run_demo.py` 里被**实际执行**验证过，不是纸上描述：

| # | 机制 | 在 Hermes 里的位置 | 演示 |
|---|---|---|---|
| 1 | AST 扫描自动发现工具，`import` 时副作用注册 | `tools/registry.py:57-74` | demo 1 |
| 2 | 同名工具冲突**默认拒绝**（不是覆盖） | `tools/registry.py:257-289` | demo 2 |
| 3 | 主循环：tool_call 解析 → 执行 → 结果回填 | `agent/conversation_loop.py` | demo 3 |
| 4 | 迭代预算**退还**（廉价工具不吃额度） | `conversation_loop.py:3281` | demo 4 |
| 5 | 技能清单进 system prompt，**正文永远走 user message** | `skills_tool.py` 注释 | demo 5 |
| 6 | 错误分类：429/5xx 退避重试，4xx 快速失败 | `agent/error_classifier.py` | demo 6 |
| 7 | 上下文压缩按**完整回合**切分，保住 tool 配对 | `agent/context_compressor.py` | demo 7 |
| 8 | 安全地板：hardline 命令连 "放开一切" 都绕不过 | `tools/approval.py:198-211` | demo 8 |
| 9 | 预算耗尽后给一次 **grace call**，且中途不施压 | `agent/agent_init.py:424`（issue #7915） | 代码 |
| 10 | 内核与呈现解耦：所有输出走回调 | `AIAgent` 的 11 个回调 | 全部 |
| 11 | 注册表 generation 计数 + 缓存失效 | `tools/registry.py` | 代码 |
| 12 | check_fn 可用性探测 + **TTL 缓存**（含刻意滞后） | `tools/registry.py:117-130` | 代码 + 测试 |
| 13 | 软引导复盘：**触发是硬的，动作是软的** | `conversation_loop.py:3964-3989` | `agent.py` |

---

## 有意简化了什么

诚实说明边界 —— 这些**没有**实现，因为它们属于 Hermes "外围极大"的那部分，
与本内核要回答的问题无关：

| 未实现 | 原因 |
|---|---|
| 多平台网关（Telegram/Discord/Slack…19+ 平台） | 属于前端层，与内核解耦 |
| TUI / ACP / dashboard | 同上 |
| 插件热加载与生命周期 hook | 需要宿主进程模型 |
| MCP 工具接入 | 需要长驻连接管理 |
| 子 agent 委派（`delegate_task`） | 需要预算父子传递的完整实现 |
| 8 种记忆后端 / 向量检索 | 只保留"文件即记忆"这一种 |
| FTS5 会话检索 | 需要 SQLite 会话库 |
| 流式响应 | 保留非流式以控制复杂度（见 `provider.py` 顶部说明） |
| 后台复盘 fork | 简化为"下一轮注入提示"，见下 |

### 一处需要说明的简化：后台复盘

Hermes 在回合结束后会 **fork 一个隔离的 AIAgent**（工具白名单只有 memory + skills）
去做复盘，因此复盘不占用当前会话的上下文。

mini-hermes 简化为：回合结束后往历史里注入一条提示消息，让**下一轮**的模型顺便考虑沉淀技能。

差别是真实的：Hermes 的复盘是隔离的、不污染上下文；这里会占用一点当前上下文。
但**关键设计保留了** —— 触发判定是确定性的硬代码，动作才是软的。

---

## 怎么加一个新工具

三步，不需要改任何清单：

```python
# minihermes/tools/my_tool.py
from minihermes.registry import register

def _my_handler(args: dict) -> str:
    return f"收到参数：{args.get('x')}"

register(
    name="my_tool",
    toolset="mytools",
    description="做一件具体的事",
    schema={
        "name": "my_tool",
        "description": "用一句话说清这个工具做什么、什么时候该用",
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        },
    },
    handler=_my_handler,
)
```

放进去就生效 —— `discover_builtin_tools()` 会 AST 扫描到它。

**注意**：工具名不能与已有工具重名（除非同 toolset 幂等，或显式 `override=True`）。
这是刻意的保守策略，防止后加载的工具静默顶掉内置工具。

---

## 已知局限

- **错误分类分支未经真实异常触发** —— 正常路径已用真实端点验证（见上），
  但 `OpenAICompatProvider` 的 429 / 上下文超限分支仍是照真实 HTTP 状态码写的，
  未在真实限流场景下触发过。
- **无并发** —— 单会话单线程，没有 Hermes 那套双消息守卫（因为这里没有网关）。
- **无持久化会话** —— 历史只在内存里，进程退出即丢失。
- **压缩摘要是规则式的** —— 不调 LLM，会丢细节（但不会因为压缩本身失败而失败）。

---

## 实跑挖出的第一手缺陷（已修）

接真实模型**第一跑**就暴露了一个 mock 测试永远测不到的问题。

**症状**：`Agent(workspace=<沙箱>)` 之后，`write_file` 把文件写进了**项目目录**，而不是沙箱。

**根因**：`file_tools` / `shell_tool` 曾把工作区写成模块级常量：

```python
WORKSPACE = Path(os.environ.get("MINIHERMES_WORKSPACE", Path.cwd())).resolve()
```

常量在 **import 那一刻**求值，而 `Agent.__init__` 是先 `discover_builtin_tools()`（触发 import）、
**后**设定工作区的 —— 于是 `WORKSPACE` 永久定格成 `cwd`，`Agent(workspace=...)` 再也改不动它。

**为什么测试没抓到**（比 bug 本身更值得记）：`TempCase.setUp` 里有一行

```python
file_tools.WORKSPACE = self.ws      # ← monkey-patch 模块常量
```

测试直接改掉了常量，把缺陷盖得严严实实。42/42 全绿，真实使用却错。

**修法**：
1. 常量 → 函数 `_workspace()`，每次调用重新解析（对齐上游 `resolve_workspace()` 的做法）；
2. `Agent.__init__` 改为**先定工作区、再发现工具**，并把 `os.environ.setdefault` 换成直接赋值
   （显式传入的 workspace 必须能覆盖环境残留）；
3. 删掉测试里的 monkey-patch，新增 `TestWorkspaceIsolation` 两个回归用例锁死。

**教训**：*mock 测试能证明逻辑自洽，不能证明集成正确。凡是"测试必须改实现内部状态才通过"的写法，
都值得怀疑。*

---

## 配套文档

本内核的每一条设计都能在上游的架构解构里找到出处：

```
G:\AI学习资料\Hermes架构白皮书\
├── 00-总览与架构全景.md      ← 六条设计铁律在这里
├── 01-Agent内核.md           ← 主循环 / 预算 / 压缩 / 错误
├── 02-工具系统.md            ← 注册表 / 安全 / 委派
├── 03-自进化闭环.md          ← 技能 / 记忆 / 复盘
└── 04-网关与插件.md          ← 前端 / 配置 / 模型接入
```
