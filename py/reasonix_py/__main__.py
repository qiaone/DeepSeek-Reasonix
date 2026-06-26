"""``python -m reasonix_py`` — minimal interactive REPL.

按 Ctrl-C / Ctrl-D / 输入 ``/quit`` 退出。``/reset`` 清空历史。
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from .agent import Agent
from .config import load_dotenv
from .provider import OpenAICompatProvider


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="reasonix-py", description=__doc__)
    parser.add_argument(
        "--reasoning-language",
        default="auto",
        choices=["auto", "zh", "en"],
        help="Visible reasoning text language (default: auto).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.7,
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=25,
        help="Max consecutive tool-call rounds in one user turn.",
    )
    args = parser.parse_args(argv)

    load_dotenv()  # best-effort

    try:
        provider = OpenAICompatProvider.from_env()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    agent = Agent(
        provider=provider,
        reasoning_language=args.reasoning_language,
        temperature=args.temperature,
        max_iterations=args.max_iterations,
    )

    print(f"Reasonix-Py REPL  (provider={provider.name}, model={provider.model})")
    print("type /quit to exit, /reset to clear history, /todos to print todo list")
    print()

    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {"/quit", "/exit"}:
            return 0
        if line == "/reset":
            agent.reset()
            print("(history cleared)")
            continue
        if line == "/todos":
            for t in agent.todos():
                print(f"  [{t.get('status', '?')}] {t.get('content', '')}")
            continue

        # 真正发问 —— 流式渲染
        print("ai> ", end="", flush=True)
        in_reasoning = False
        for ev in agent.run(line):
            if ev.kind == "reasoning":
                if not in_reasoning:
                    print("\n[thinking] ", end="", flush=True)
                    in_reasoning = True
                print(ev.text, end="", flush=True)
            elif ev.kind == "text":
                if in_reasoning:
                    print("\n[answer] ", end="", flush=True)
                    in_reasoning = False
                print(ev.text, end="", flush=True)
            elif ev.kind == "tool_call":
                if ev.extra.get("phase") == "start" and ev.tool_call:
                    print(f"\n[tool->] {ev.tool_call.name}", end="", flush=True)
            elif ev.kind == "tool_result":
                if ev.tool_result:
                    preview = ev.tool_result.output.replace("\n", " ⏎ ")[:120]
                    flag = " ERR" if ev.tool_result.is_error else ""
                    print(f"\n[<-tool{flag}] {preview}", flush=True)
            elif ev.kind == "usage":
                pass  # 静默；想看 token 用量请改这里
            elif ev.kind == "error":
                print(f"\n[error] {ev.error!r}", file=sys.stderr)
            elif ev.kind == "turn_end":
                print()  # 终止换行
        print()


if __name__ == "__main__":
    raise SystemExit(main())
