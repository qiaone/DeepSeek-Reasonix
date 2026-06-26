"""Hello-world style example.

Usage::

    set REASONIX_API_KEY=...
    set REASONIX_BASE_URL=https://api.deepseek.com/v1
    set REASONIX_MODEL=deepseek-chat
    python examples\\hello.py
"""

from reasonix_py import Agent, OpenAICompatProvider
from reasonix_py.config import load_dotenv


def main() -> None:
    load_dotenv()
    provider = OpenAICompatProvider.from_env()
    agent = Agent(provider=provider, reasoning_language="zh")

    prompt = "用 ls 工具看看当前目录有什么文件，然后用一句话告诉我。"
    print(f"USER> {prompt}\n")
    for ev in agent.run(prompt):
        if ev.kind == "text":
            print(ev.text, end="", flush=True)
        elif ev.kind == "tool_call" and ev.extra.get("phase") == "start" and ev.tool_call:
            print(f"\n[call] {ev.tool_call.name}", flush=True)
        elif ev.kind == "tool_result" and ev.tool_result:
            print(f"[result] {ev.tool_result.output[:200]}\n", flush=True)
        elif ev.kind == "error":
            print(f"\n[error] {ev.error!r}")
    print()


if __name__ == "__main__":
    main()
