"""
mini-hermes 冒烟测试。

    python -m unittest discover -s tests -v

覆盖：注册表语义、工具发现、主循环、参数容错、预算、压缩、错误分类、
路径安全、安全地板、技能与记忆读写。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试会故意走异常路径（工具抛错、注册冲突、探测失败），
# 屏蔽日志让输出只剩下真正的测试结果。
logging.disable(logging.CRITICAL)

from minihermes.agent import Agent                                  # noqa: E402
from minihermes.budget import IterationBudget                       # noqa: E402
from minihermes.context import ContextCompressor, estimate_tokens   # noqa: E402
from minihermes.errors import ErrorKind, classify, should_retry     # noqa: E402
from minihermes.memory import MemoryStore                           # noqa: E402
from minihermes.provider import (                                   # noqa: E402
    LLMError,
    LLMResponse,
    MockProvider,
    tool_call,
)
from minihermes.registry import ToolRegistry, discover_builtin_tools, registry  # noqa: E402
from minihermes.skills import SkillStore, parse_frontmatter         # noqa: E402


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="minihermes-test-")
        self.tmp = Path(self._tmp.name)
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        # 工作区通过**环境变量**生效 —— 这正是 Agent.__init__ 做的事。
        # ⚠️ 早前这里直接写 file_tools.WORKSPACE = self.ws（monkey-patch 模块常量），
        #    结果让"常量在 import 时被冻结"这个真实缺陷永远测不出来。
        #    修好后测试只需设环境变量，缺陷由 TestWorkspaceIsolation 守住。
        os.environ["MINIHERMES_WORKSPACE"] = str(self.ws)

    def tearDown(self) -> None:
        os.environ.pop("MINIHERMES_WORKSPACE", None)
        self._tmp.cleanup()

    def make_agent(self, script, **kw) -> Agent:
        return Agent(
            provider=MockProvider(script=script),
            workspace=self.ws,
            skills_root=self.tmp / "skills",
            memory_root=self.tmp / "mem",
            **kw,
        )


# ══════════════════════════════════════════════════════════════ 注册表

class TestRegistry(unittest.TestCase):
    def test_duplicate_different_toolset_rejected(self) -> None:
        reg = ToolRegistry()
        schema = {"name": "t", "description": "", "parameters": {"type": "object"}}
        self.assertTrue(reg.register(name="t", toolset="a", schema=schema, handler=lambda a: ""))
        self.assertFalse(reg.register(name="t", toolset="b", schema=schema, handler=lambda a: ""))

    def test_duplicate_same_toolset_is_idempotent(self) -> None:
        reg = ToolRegistry()
        schema = {"name": "t", "description": "", "parameters": {"type": "object"}}
        reg.register(name="t", toolset="a", schema=schema, handler=lambda a: "1")
        self.assertTrue(reg.register(name="t", toolset="a", schema=schema, handler=lambda a: "2"))
        self.assertEqual(reg.dispatch("t", {}), "2")

    def test_override_true_allows_shadowing(self) -> None:
        reg = ToolRegistry()
        schema = {"name": "t", "description": "", "parameters": {"type": "object"}}
        reg.register(name="t", toolset="a", schema=schema, handler=lambda a: "old")
        self.assertTrue(
            reg.register(name="t", toolset="b", schema=schema, handler=lambda a: "new", override=True)
        )
        self.assertEqual(reg.dispatch("t", {}), "new")

    def test_generation_bumps_on_mutation(self) -> None:
        reg = ToolRegistry()
        g0 = reg.generation
        schema = {"name": "t", "description": "", "parameters": {"type": "object"}}
        reg.register(name="t", toolset="a", schema=schema, handler=lambda a: "")
        self.assertGreater(reg.generation, g0)

    def test_check_fn_gates_availability(self) -> None:
        reg = ToolRegistry()
        schema = {"name": "t", "description": "", "parameters": {"type": "object"}}
        flag = {"on": False}
        reg.register(
            name="t", toolset="a", schema=schema,
            handler=lambda a: "ran",
            check_fn=lambda: flag["on"],
        )
        self.assertEqual(reg.get_definitions(), [])
        self.assertIn("不可用", reg.dispatch("t", {}))
        flag["on"] = True
        # check_fn 结果有 TTL 缓存（刻意设计，避免每轮都做 IO 探测），
        # 所以状态变化后要么等 TTL 过期，要么主动清缓存。
        reg.refresh_availability()
        self.assertEqual(len(reg.get_definitions()), 1)

    def test_check_fn_exception_marks_unavailable(self) -> None:
        reg = ToolRegistry()
        schema = {"name": "t", "description": "", "parameters": {"type": "object"}}

        def boom() -> bool:
            raise RuntimeError("probe failed")

        reg.register(name="t", toolset="a", schema=schema, handler=lambda a: "", check_fn=boom)
        self.assertFalse(reg.is_available("t"))

    def test_dispatch_unknown_tool_returns_error_text(self) -> None:
        reg = ToolRegistry()
        self.assertIn("不存在", reg.dispatch("nope", {}))

    def test_handler_exception_is_wrapped(self) -> None:
        reg = ToolRegistry()
        schema = {"name": "t", "description": "", "parameters": {"type": "object"}}

        def boom(args):
            raise ValueError("bad")

        reg.register(name="t", toolset="a", schema=schema, handler=boom)
        self.assertIn("ValueError", reg.dispatch("t", {}))

    def test_result_truncation(self) -> None:
        reg = ToolRegistry()
        schema = {"name": "t", "description": "", "parameters": {"type": "object"}}
        reg.register(
            name="t", toolset="a", schema=schema,
            handler=lambda a: "x" * 500, max_result_chars=100,
        )
        out = reg.dispatch("t", {})
        self.assertIn("结果被截断", out)


# ══════════════════════════════════════════════════════════════ 工具发现

class TestDiscovery(unittest.TestCase):
    def test_builtin_tools_discovered(self) -> None:
        discover_builtin_tools()
        names = registry.available_names()
        for expected in ("read_file", "write_file", "run_shell", "skills_list", "memory_append"):
            self.assertIn(expected, names)

    def test_toolsets_grouped(self) -> None:
        discover_builtin_tools()
        by_set = registry.tools_by_toolset()
        self.assertIn("file", by_set)
        self.assertIn("shell", by_set)


# ══════════════════════════════════════════════════════════════ 主循环

class TestLoop(TempCase):
    def test_simple_text_turn(self) -> None:
        agent = self.make_agent([LLMResponse(content="你好")])
        result = agent.chat("在吗")
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.text, "你好")
        self.assertEqual(result.iterations, 0)

    def test_tool_call_roundtrip(self) -> None:
        (self.ws / "a.txt").write_text("内容ABC", encoding="utf-8")
        agent = self.make_agent([
            LLMResponse(tool_calls=[tool_call("read_file", path="a.txt")]),
            LLMResponse(content="读到了"),
        ])
        result = agent.chat("读一下 a.txt")
        self.assertEqual(result.iterations, 1)
        tool_msgs = [m for m in agent.loop.history if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertIn("内容ABC", tool_msgs[0]["content"])

    def test_bad_json_arguments_do_not_crash(self) -> None:
        from minihermes.provider import ToolCall

        agent = self.make_agent([
            LLMResponse(tool_calls=[ToolCall(id="x", name="read_file", arguments="{not json")]),
            LLMResponse(content="我重试"),
        ])
        result = agent.chat("读文件")
        self.assertEqual(result.stop_reason, "completed")
        tool_msgs = [m for m in agent.loop.history if m.get("role") == "tool"]
        self.assertIn("不是合法 JSON", tool_msgs[0]["content"])

    def test_message_pairing_intact(self) -> None:
        (self.ws / "a.txt").write_text("x", encoding="utf-8")
        agent = self.make_agent([
            LLMResponse(tool_calls=[tool_call("read_file", path="a.txt"), tool_call("list_dir", path=".")]),
            LLMResponse(content="done"),
        ])
        agent.chat("看看")
        pending: set[str] = set()
        for msg in agent.loop.history:
            if msg.get("role") == "assistant":
                for c in msg.get("tool_calls") or []:
                    pending.add(c["id"])
            elif msg.get("role") == "tool":
                pending.discard(msg["tool_call_id"])
        self.assertEqual(pending, set())

    def test_memory_injected_into_user_message_not_system(self) -> None:
        agent = self.make_agent([LLMResponse(content="ok")])
        agent.memory.write("memory", "项目约定：产物放子文件夹")
        sys_prompt_before = agent.loop.system_prompt
        agent.chat("继续")
        user_msgs = [m for m in agent.loop.history if m.get("role") == "user"]
        self.assertIn("产物放子文件夹", user_msgs[0]["content"])
        self.assertEqual(agent.loop.system_prompt, sys_prompt_before)
        self.assertNotIn("产物放子文件夹", agent.loop.system_prompt)


# ══════════════════════════════════════════════════════════════ 预算

class TestBudget(unittest.TestCase):
    def test_exhaust_and_refund(self) -> None:
        b = IterationBudget(limit=3)
        b.consume()
        b.consume()
        self.assertEqual(b.remaining, 1)
        b.refund()
        self.assertEqual(b.used, 1)

    def test_child_charges_parent(self) -> None:
        parent = IterationBudget(limit=10)
        child = parent.child(limit=2)
        child.consume()
        self.assertEqual(parent.used, 1)
        self.assertEqual(child.used, 1)

    def test_cheap_tools_refund_in_loop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "ws"
            ws.mkdir()
            agent = Agent(
                provider=MockProvider(script=[
                    LLMResponse(tool_calls=[tool_call("skills_list")]),
                    LLMResponse(content="ok"),
                ]),
                workspace=ws,
                skills_root=tmp / "skills",
                memory_root=tmp / "mem",
            )
            agent.chat("列技能")
            self.assertEqual(agent.budget.used, 0)


# ══════════════════════════════════════════════════════════════ 压缩

class TestCompression(unittest.TestCase):
    def _build(self, turns: int) -> list[dict]:
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "任务开始"}]
        for i in range(turns):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            ]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 300})
            msgs.append({"role": "user", "content": f"继续 {i}"})
        return msgs

    def test_no_compression_when_small(self) -> None:
        c = ContextCompressor(max_tokens=1_000_000)
        msgs = self._build(3)
        self.assertFalse(c.compress(msgs).compressed)

    def test_compression_preserves_pairing(self) -> None:
        c = ContextCompressor(max_tokens=500, keep_recent_turns=2)
        result = c.compress(self._build(10))
        self.assertTrue(result.compressed)

        pending: set[str] = set()
        for msg in result.messages:
            if msg.get("role") == "assistant":
                for call in msg.get("tool_calls") or []:
                    pending.add(call["id"])
            elif msg.get("role") == "tool":
                self.assertIn(msg["tool_call_id"], pending)
                pending.discard(msg["tool_call_id"])
        self.assertEqual(pending, set())

    def test_keeps_system_and_first_turn(self) -> None:
        c = ContextCompressor(max_tokens=500, keep_recent_turns=2)
        result = c.compress(self._build(10))
        self.assertEqual(result.messages[0]["role"], "system")
        self.assertEqual(result.messages[1]["content"], "任务开始")

    def test_too_few_turns_skips(self) -> None:
        c = ContextCompressor(max_tokens=1, keep_recent_turns=4)
        self.assertFalse(c.compress(self._build(2)).compressed)

    def test_estimate_tokens_positive(self) -> None:
        self.assertGreater(estimate_tokens([{"role": "user", "content": "你好世界"}]), 0)


# ══════════════════════════════════════════════════════════════ 错误

class TestErrors(unittest.TestCase):
    def test_classification(self) -> None:
        self.assertEqual(classify(LLMError("x", 429)), ErrorKind.RATE_LIMIT)
        self.assertEqual(classify(LLMError("x", 500)), ErrorKind.RETRYABLE)
        self.assertEqual(classify(LLMError("x", 401)), ErrorKind.AUTH)
        self.assertEqual(classify(LLMError("maximum context length", 400)), ErrorKind.CONTEXT_OVERFLOW)
        self.assertEqual(classify(LLMError("bad param", 422)), ErrorKind.BAD_REQUEST)
        self.assertEqual(classify(LLMError("网络错误: timeout")), ErrorKind.RETRYABLE)

    def test_retry_policy(self) -> None:
        self.assertTrue(should_retry(ErrorKind.RATE_LIMIT, 1, 3))
        self.assertFalse(should_retry(ErrorKind.RATE_LIMIT, 3, 3))
        self.assertFalse(should_retry(ErrorKind.BAD_REQUEST, 1, 3))

    def test_retry_then_success(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "ws"
            ws.mkdir()
            agent = Agent(
                provider=MockProvider(script=[
                    LLMError("HTTP 429", 429),
                    LLMResponse(content="恢复了"),
                ]),
                workspace=ws,
                skills_root=tmp / "skills",
                memory_root=tmp / "mem",
            )
            result = agent.chat("问点什么")
            self.assertEqual(result.text, "恢复了")


# ══════════════════════════════════════════════════════════════ 路径安全/地板

class TestSafety(TempCase):
    def test_path_escape_blocked(self) -> None:
        from minihermes.tools.file_tools import _safe_path

        # 底层守卫：越界是安全违规，走异常路径
        with self.assertRaises(PermissionError):
            _safe_path("../../../../etc/passwd")

        # 真实调用路径：注册表把异常包装成给模型看的错误文本，不会炸掉会话
        discover_builtin_tools()
        out = registry.dispatch("read_file", {"path": "../../secret.txt"})
        self.assertIn("越界", out)

    def test_hardline_shell_blocked(self) -> None:
        from minihermes.tools.shell_tool import _run_shell

        for cmd in ("rm -rf /", "mkfs.ext4 /dev/sda", "shutdown -h now"):
            out = _run_shell({"command": cmd})
            self.assertIn("安全地板", out, msg=cmd)

    def test_dangerous_shell_blocked_by_default(self) -> None:
        from minihermes.tools.shell_tool import _run_shell

        out = _run_shell({"command": "sudo rm something"})
        self.assertIn("危险操作", out)


# ══════════════════════════════════════════════════════════ 工作区隔离（回归）

class TestWorkspaceIsolation(TempCase):
    """回归：Agent(workspace=...) 必须让 file/shell 工具真正锚定到该工作区。

    历史缺陷：工作区被写成模块级常量，在 import 那一刻就冻结成 cwd，
    Agent 随后传入的 workspace 无声失效 —— 真实模型验证时 write_file
    把文件落到了项目目录而不是指定沙箱。
    """

    def test_file_tool_follows_agent_workspace(self) -> None:
        from minihermes.tools.file_tools import _safe_path

        discover_builtin_tools()
        target = self.tmp / "target"
        target.mkdir()
        # 先塞一个"过期"工作区，模拟真实进程里的环境残留
        os.environ["MINIHERMES_WORKSPACE"] = str(Path.cwd())

        Agent(
            provider=MockProvider(),
            workspace=target,
            skills_root=self.tmp / "s",
            memory_root=self.tmp / "m",
        )
        self.assertEqual(_safe_path("a.txt").parent, target.resolve())
        self.assertEqual(_safe_path("deep/b.txt").parent, (target / "deep").resolve())

    def test_shell_tool_cwd_follows_agent_workspace(self) -> None:
        from minihermes.tools.shell_tool import _workspace

        target = self.tmp / "target2"
        target.mkdir()
        os.environ["MINIHERMES_WORKSPACE"] = str(Path.cwd())

        Agent(
            provider=MockProvider(),
            workspace=target,
            skills_root=self.tmp / "s2",
            memory_root=self.tmp / "m2",
        )
        self.assertEqual(_workspace(), target.resolve())


# ══════════════════════════════════════════════════════════════ 技能

class TestSkills(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SkillStore(Path(self._tmp.name) / "skills")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_frontmatter_parse(self) -> None:
        fm, body = parse_frontmatter("---\nname: a\ndescription: b\n---\n\n正文")
        self.assertEqual(fm["name"], "a")
        self.assertEqual(body.strip(), "正文")

    def test_create_and_read(self) -> None:
        msg = self.store.create("my-skill", "做某事", "## 步骤\n1. 第一步")
        self.assertIn("已创建", msg)
        self.assertEqual(len(self.store.list_meta()), 1)
        self.assertIn("第一步", self.store.get_body("my-skill"))

    def test_invalid_name_rejected(self) -> None:
        self.assertIn("非法", self.store.create("Bad Name!", "d", "b"))
        self.assertIn("非法", self.store.create("x" * 70, "d", "b"))

    def test_description_required(self) -> None:
        self.assertIn("description 必填", self.store.create("ok-name", "", "b"))

    def test_duplicate_create_rejected(self) -> None:
        self.store.create("dup", "d", "b")
        self.assertIn("已存在", self.store.create("dup", "d", "b"))

    def test_patch(self) -> None:
        self.store.create("p", "d", "## 步骤\n1. 旧步骤\n2. 保留")
        msg = self.store.patch("p", "1. 旧步骤", "1. 新步骤")
        self.assertIn("已更新", msg)
        body = self.store.get_body("p")
        self.assertIn("新步骤", body)
        self.assertIn("保留", body)

    def test_patch_missing_fragment_reports(self) -> None:
        self.store.create("p2", "d", "abc")
        self.assertIn("找不到", self.store.patch("p2", "不存在的内容", "x"))

    def test_catalog_has_name_not_body(self) -> None:
        self.store.create("cat", "描述在这里", "## 秘密正文")
        block = self.store.catalog_block()
        self.assertIn("cat", block)
        self.assertNotIn("秘密正文", block)


# ══════════════════════════════════════════════════════════════ 记忆

class TestMemory(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self._tmp.name) / "mem")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_and_read(self) -> None:
        self.store.append("memory", "第一条")
        self.store.append("memory", "第二条")
        content = self.store.read("memory")
        self.assertIn("第一条", content)
        self.assertIn("第二条", content)

    def test_unknown_scope(self) -> None:
        with self.assertRaises(ValueError):
            self.store.read("nope")

    def test_size_limit_enforced(self) -> None:
        out = self.store.write("memory", "x" * 20_000)
        self.assertIn("超过", out)

    def test_context_block_empty_when_blank(self) -> None:
        self.assertEqual(self.store.context_block(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
