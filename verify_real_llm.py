"""
verify_real_llm.py —— 用**真实模型端点**跑通 mini-hermes

demo 与单测都由 MockProvider 驱动，只能证明"代码逻辑对"，不能证明"接真模型能跑"。
这个脚本补上那一环：接一个真实的 OpenAI 兼容端点，跑完整回合。

密钥来源：默认从原版 Hermes 的 .env 借 DEEPSEEK_API_KEY（只读取，不打印）。
也可以自己设置环境变量后直接跑：
    export MINIHERMES_API_KEY=sk-xxx
    export MINIHERMES_BASE_URL=https://api.deepseek.com/v1
    export MINIHERMES_MODEL=deepseek-chat
    python verify_real_llm.py

运行：
    python verify_real_llm.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ── 让脚本无论从哪个目录启动都能 import 到包 ─────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minihermes.agent import Agent  # noqa: E402
from minihermes.callbacks import Callbacks  # noqa: E402
from minihermes.provider import build_provider  # noqa: E402

HERMES_ENV = Path("G:/Hermes/.hermes/.env")


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def load_borrowed_key() -> tuple[str, str, str] | None:
    """从原版 Hermes 的 .env 借一个 OpenAI 兼容后端。返回 (key, base_url, model)。"""
    if os.environ.get("MINIHERMES_API_KEY"):
        return (
            os.environ["MINIHERMES_API_KEY"],
            os.environ.get("MINIHERMES_BASE_URL", "https://api.deepseek.com/v1"),
            os.environ.get("MINIHERMES_MODEL", "deepseek-chat"),
        )
    if not HERMES_ENV.exists():
        return None

    cfg: dict[str, str] = {}
    for line in HERMES_ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip().strip('"').strip("'")

    ds = cfg.get("DEEPSEEK_API_KEY", "")
    if ds:
        return (ds, "https://api.deepseek.com/v1", "deepseek-chat")

    # 兜底：任何看起来像 OpenAI 兼容的 key + 显式 base_url 组合
    for key_name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "GLM_API_KEY",
                     "KIMI_API_KEY", "MINIMAX_API_KEY"):
        if cfg.get(key_name):
            base = cfg.get(f"{key_name}_BASE_URL", "")
            if base:
                return (cfg[key_name], base, os.environ.get("MINIHERMES_MODEL", "gpt-4o-mini"))
    return None


def main() -> int:
    _force_utf8()

    borrowed = load_borrowed_key()
    if borrowed is None:
        print("⚠️  没有找到可用的 OpenAI 兼容密钥，无法做真实模型验证。")
        print("   请设置 MINIHERMES_API_KEY / MINIHERMES_BASE_URL / MINIHERMES_MODEL 后重跑。")
        return 2

    api_key, base_url, model = borrowed
    os.environ["MINIHERMES_API_KEY"] = api_key
    os.environ["MINIHERMES_BASE_URL"] = base_url
    os.environ["MINIHERMES_MODEL"] = model

    print("═" * 68)
    print("  mini-hermes × 真实模型验证")
    print("═" * 68)
    print(f"  端点   : {base_url}")
    print(f"  模型   : {model}")
    print(f"  密钥   : {'*' * 8}{api_key[-4:]}（从原版 Hermes .env 借，未打印明文）")
    print()

    # 独立沙箱目录，绝不碰用户的真实工作区
    tmp = Path(tempfile.mkdtemp(prefix="minihermes-real-"))
    events: list[str] = []

    def on_tool_start(name: str, args: dict) -> None:
        events.append(name)
        import json as _json

        shown = _json.dumps(args, ensure_ascii=False)[:110]
        print(f"  🔧 调用工具 {name}  {shown}")

    def on_tool_end(name: str, result: str) -> None:
        print(f"     ↳ 结果 {len(result)} 字符：{result.strip()[:90]}")

    cb = Callbacks(on_tool_start=on_tool_start, on_tool_end=on_tool_end)

    agent = Agent(
        provider=build_provider(model),
        model=model,
        workspace=tmp,
        skills_root=tmp / "skills",
        memory_root=tmp / "memory",
        max_iterations=8,
        callbacks=cb,
    )

    prompts = [
        "你是什么？用一句话回答。",
        "请调用 write_file 在当前工作目录创建 hello.txt，内容写：mini-hermes 用真实模型跑通了。",
        "读一下刚创建的 hello.txt，把内容原样告诉我。",
    ]

    for i, p in enumerate(prompts, 1):
        print(f"\n─── 第 {i} 轮 ─────────────────────────────────────────────")
        print(f"  用户：{p}")
        try:
            result = agent.chat(p)
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ 回合异常：{type(e).__name__}: {e}")
            return 1
        print(f"  助手：{(result.text or '').strip()[:400]}")
        print(f"  （本轮 {result.iterations} 次模型调用 / 预算剩 {agent.budget.remaining}）")

    print("\n" + "═" * 68)
    created = tmp / "hello.txt"
    if created.exists():
        print(f"  ✅ 真实模型通过工具真的落盘了文件：{created.name}")
        print(f"     内容：{created.read_text(encoding='utf-8').strip()!r}")
    else:
        print("  ⚠️ hello.txt 未生成 —— 模型可能没按指令调工具（不影响主循环结论）")
    print(f"  本轮共发生 {len(events)} 次工具调用：{events}")
    print("═" * 68)
    print(f"  沙箱目录（可删）：{tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
