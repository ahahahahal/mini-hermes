# 架构对照：mini-hermes ↔ Hermes Agent

这份文档把白皮书里的**六条设计铁律**逐条映射到 mini-hermes 的实际代码位置，
并说明每一处的简化取舍。

它的用途：**想知道某个设计为什么这么写时，来这张表里查**。

---

## 一、六条铁律的落点

### 铁律一：Prompt 缓存不可破坏

> 任何"每轮可能不同"的东西都不许进 system prompt。

| 体现 | 位置 |
|---|---|
| 记忆拼进 **user message** 的开头，而不是 system prompt | `loop.py::_wrap_user_input()` |
| 技能正文经 `loop.inject()` 作为 **user message** 注入 | `agent.py::_maybe_expand_skill()` |
| 技能清单（name + description）才进 system prompt | `agent.py::build_system_prompt()` |
| system prompt 在会话期间保持恒定 | `loop.py::_assembled()` |
| 工具集在会话开始时取一次并缓存（中途变会破坏缓存） | `loop.py::__init__` 的 `self._tool_defs` |
| 技能增删后需**显式**刷新，不会自动发生 | `agent.py::refresh_system_prompt()` |

**验证方式**：`tests/test_smoke.py::TestLoop::test_memory_injected_into_user_message_not_system`
断言了记忆出现在 user 消息里、且 system prompt 前后完全一致。

**简化取舍**：Hermes 的技能清单刷新是**延迟失效 + `--now` 显式触发**；这里保留"显式刷新"
的语义，但没有实现延迟队列。

---

### 铁律二：插件不得修改核心

| 体现 | 位置 |
|---|---|
| 工具通过注册表接入，内核不认识任何具体工具 | `registry.py` |
| 注册表零内部依赖，是依赖链的起点 | `registry.py` 文件头 |
| 新增工具 = 新增文件，不改任何清单 | `registry.py::discover_builtin_tools()` |

**简化取舍**：Hermes 还有 `register(ctx)` 插件门面 + 六个生命周期 hook
（`pre_tool_call` / `post_tool_call` / `pre_llm_call` / …）。这里没有 hook 机制，
因为单进程无插件的场景下它没有落点。

---

### 铁律三：绝不删除，最多归档

| 体现 | 位置 |
|---|---|
| 技能只支持 create / patch，**没有 delete 动作** | `tools/skill_tools.py::_skill_manage` |
| 记忆超限时**拒绝写入并提示精简**，而不是静默截断 | `memory.py::write()` |

**简化取舍**：Hermes 有完整的 Curator 状态机（`active → stale → archived`，
30 天 / 90 天阈值）+ tar.gz 快照回滚。这里只保留了"没有删除动作"这个最小的安全底线。

---

### 铁律四：内核与呈现彻底解耦

| 体现 | 位置 |
|---|---|
| 内核不 `print` 任何用户可见内容，全部走回调 | `callbacks.py` |
| 回调抛异常也不能炸内核 | `callbacks.py::emit()` |
| CLI 只是回调的一个消费者，内核不知道终端存在 | `cli.py::make_callbacks()` |

**简化取舍**：Hermes 有 11 个回调（含 `stream_delta` / `reasoning` / `clarify` 等），
这里保留 6 个最必要的。

---

### 铁律五：Profile 隔离

| 体现 | 位置 |
|---|---|
| 一切状态之根是 `MINIHERMES_HOME`，不硬编码 `~/.minihermes` | `skills.py::default_skills_root()`、`memory.py::default_memory_root()` |
| 工作目录由 `MINIHERMES_WORKSPACE` 决定 | `tools/file_tools.py::WORKSPACE` |
| 测试通过注入临时目录实现完全隔离 | `tests/test_smoke.py::TempCase` |

**简化取舍**：Hermes 的 `_apply_profile_override()` 必须在**任何 hermes 模块 import 之前**
执行（时序敏感）。这里用环境变量在进程启动时读取，没有 profile 切换能力。

---

### 铁律六：并发守卫必须同步安装

**mini-hermes 未实现** —— 因为它没有网关，单会话单线程，不存在"同一会话并发两条消息"的场景。

Hermes 的双守卫（适配器级 `_active_sessions` + Runner 级 `_running_agents` 哨兵）
是为了解决 `await` 间隙里的竞态：守卫检查与 agent 创建之间有多处 await
（hook、视觉增强、STT、会话压缩），哨兵必须**在任何 await 之前**同步写入。

要复刻这一条，需要先有网关。留给下一步。

---

## 二、机制级对照表

| 机制 | Hermes 源码位置 | mini-hermes | 差异 |
|---|---|---|---|
| 工具自动发现 | `tools/registry.py:57-74` | `registry.py::discover_builtin_tools` | Hermes 扫描含 MCP 刷新；这里只扫描内置 |
| 注册冲突策略 | `tools/registry.py:257-289` | `registry.py::register` | 一致（同 toolset 幂等 / 双 mcp / override / 其余拒绝） |
| check_fn 缓存 | 30s TTL | 同 | Hermes 用 generation keyed cache；这里用单调 TTL |
| 工具结果截断 | `tool_output_limits` + `tool_result_storage` 三层 | `registry.py::dispatch` 单层 | 少了"超限落盘"这一层 |
| 主循环 | `agent/conversation_loop.py:85`（约 4018 行单函数） | `loop.py::run()` ~150 行 | 功能子集，结构同形 |
| 中断 | 三响应点 + 线程级信号集合 | `threading.Event` + 三响应点 | 少了跨线程信号投递 |
| 预算 | `IterationBudget`（父子共享） | `budget.py` 同 | 一致 |
| 预算退还 | `conversation_loop.py:3281` | `loop.py` 的 `cheap_only` 分支 | 一致的思路，工具集换成 skills/memory |
| grace call | 预算耗尽注入一次提示 | `loop.py` 的 `_GRACE_HINT` | 一致，且同样"中途不施压" |
| 上下文压缩 | `agent/context_compressor.py`（LLM 摘要） | `context.py`（规则摘要） | 这里不调 LLM |
| 错误分类 | `agent/error_classifier.py` | `errors.py::classify` | 覆盖主要类别，分类规则更简单 |
| 模型降级 | `fallback_model` | `fallback_provider` | 一致 |
| 技能格式 | SKILL.md + references/templates/scripts | 同（解析器手写，不依赖 PyYAML） | 只解析 frontmatter 的扁平子集 |
| 技能加载 | 正文经 user message 注入 | 同 | 一致 |
| 记忆后端 | 8 种（honcho/mem0/…） | 1 种（文件） | — |
| 会话检索 | SQLite + FTS5 双表（含 CJK trigram） | 未实现 | — |
| 后台复盘 | fork 隔离 AIAgent（白名单 memory+skills） | 下一轮注入提示 | 见 README「一处需要说明的简化」 |

---

## 三、数据流：一个回合的完整路径

```
Agent.chat(text)
  │
  ├─ _maybe_expand_skill(text)          # /技能名 → loop.inject(技能正文)
  │
  └─ ConversationLoop.run(text)
       │
       ├─ _wrap_user_input()            # 记忆 → 拼进 user message（不是 system prompt！）
       │
       └─ while True:
            ├─ 中断检查                        ← 响应点 1
            ├─ 压缩检查（唯一改写历史的时刻）
            ├─ 预算检查 → grace call
            ├─ _call_with_retries()          # classify → 重试/压缩/降级
            ├─ 中断检查                        ← 响应点 2
            ├─ 无 tool_calls → return 结束
            └─ for call in tool_calls:
                 ├─ 中断检查                   ← 响应点 3
                 ├─ _parse_args()             # 坏 JSON → 错误文本（不崩）
                 ├─ registry.dispatch()       # 工具执行 + 异常包装 + 结果截断
                 └─ 回填 role="tool" 消息
                 ※ 本轮全是廉价工具 → budget.refund()
       │
       └─ Agent._maybe_nudge()            # 硬触发判定，软提示动作
```

---

## 四、有意偏离上游的地方

| 偏离 | 理由 |
|---|---|
| 主循环写成类方法而非 4000 行单函数 | 可读性。Hermes 的单函数是历史包袱，不是设计目标 |
| 压缩用规则摘要而非 LLM | 压缩本身不该有失败路径 |
| 未实现事件 hook 系统 | 单进程无插件场景下无落点 |
| 未实现双消息守卫 | 没有网关，无并发场景 |
| `MockProvider` 一等公民 | 让"能跑"不依赖任何外部条件 |
| 前端只有 CLI | 证明解耦用 1 个前端就够，不需要 4 个 |

---

## 五、如果继续往下做

按价值排序，下一步最值得补的三件事：

1. **接真实模型跑通**（成本最低，验证最大）—— `OpenAICompatProvider` 已经写好，
   缺的只是真实 key 与一次真实 429 场景的检验。
2. **会话持久化 + FTS5 检索** —— 补齐"情景记忆"这一半，让跨会话回忆成立。
3. **后台复盘 fork** —— 把软引导升级成隔离复盘，让自进化闭环真正闭环。

再往后才是：插件 hook、双守卫、多前端、MCP。
