"""Agent main loop — the heart of Reasonix-Py.

对应 Go ``internal/agent/agent.go``。这里只实现一个干净的"流式 → 收集 tool_calls →
执行 → 追加 → 再循环"骨架，足以驱动一个能用的 coding agent。

省略的内容（与 Go 版相比）：
- plan / goal / coordinator / subagent / cancel / branch / compaction
- output style / memory / skills / token-economy
- checkpoint / 时光机
- 复杂的 cache-shape 校验

但保留了若干**硬约束**：

1. ``build_system_prompt`` 永远把 ``UserDecisionPolicy`` 与 ``LanguagePolicy`` 追加到末尾，
   即使用户传了自定义 base prompt（与 ``boot.Build`` 逻辑一致）。
2. system prompt 在一个 ``Agent`` 实例的整个生命周期内**字节稳定**，构造完不再修改。
3. 一批 tool_calls 全部 ``read_only=True`` 时才并行；混合或 write 工具串行（与 Go 版一致）。
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from .messages import Message, Role, ToolCall
from .prompts import (
    DEFAULT_SYSTEM_PROMPT,
    LANGUAGE_POLICY,
    USER_DECISION_POLICY,
    reasoning_language_block,
)
from .provider import (
    Chunk,
    ChunkType,
    Provider,
    Request,
    StreamInterruptedError,
)
from .tools import Registry, Tool, ToolResult
from .builtins import default_registry

log = logging.getLogger("reasonix_py.agent")


# ---------------------------------------------------------------------------
# system prompt assembly
# ---------------------------------------------------------------------------


def build_system_prompt(
    *,
    base: Optional[str] = None,
    extras: Optional[list[str]] = None,
) -> str:
    """Assemble the cache-stable system prompt.

    顺序（与 Go ``boot.Build`` 对齐）::

        base (DefaultSystemPrompt 或用户覆盖)
        \\n\\n + UserDecisionPolicy        ← 永远追加
        \\n\\n + LanguagePolicy            ← 永远追加
        \\n\\n + extra_1
        \\n\\n + extra_2 ...               ← 后续可放 outputstyle / memory / skills

    Parameters
    ----------
    base:
        ``None`` 时使用 :data:`DEFAULT_SYSTEM_PROMPT`。
    extras:
        额外要追加的 block（按顺序）。
    """
    parts: list[str] = [base if base is not None else DEFAULT_SYSTEM_PROMPT]
    parts.append(USER_DECISION_POLICY)
    parts.append(LANGUAGE_POLICY)
    if extras:
        parts.extend(extras)
    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# agent events (what ``Agent.run`` yields)
# ---------------------------------------------------------------------------


@dataclass
class AgentEvent:
    """High-level event yielded by :meth:`Agent.run`.

    Frontend / CLI 直接根据 ``kind`` 决定如何渲染。
    """

    kind: str       # "text" | "reasoning" | "tool_call" | "tool_result" | "usage" | "error" | "turn_end"
    text: str = ""
    tool_call: Optional[ToolCall] = None
    tool_result: Optional[ToolResult] = None
    error: Optional[BaseException] = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    """Stateful agent: holds history, tools, and runs the streaming loop.

    Parameters
    ----------
    provider:
        An instance of :class:`reasonix_py.provider.Provider`.
    tools:
        A :class:`Registry`. If ``None``, builds a default registry with the 5 built-ins.
    system_prompt:
        Override the *base* of the system prompt. ``UserDecisionPolicy`` and
        ``LanguagePolicy`` are still appended.
    extras:
        Extra system blocks to append (e.g. project memory, skill index).
    reasoning_language:
        ``"auto" | "zh" | "en"`` —— 注入到**每个**用户回合最前面的 ``<reasoning-language>`` 块。
    max_iterations:
        最多连续多少轮工具调用。超过则中止（避免死循环）。
    """

    def __init__(
        self,
        provider: Provider,
        *,
        tools: Optional[Registry] = None,
        system_prompt: Optional[str] = None,
        extras: Optional[list[str]] = None,
        reasoning_language: str = "auto",
        max_iterations: int = 25,
        temperature: float = 0.7,
    ) -> None:
        self.provider = provider
        self._todos: list[dict[str, Any]] = []
        if tools is None:
            tools = default_registry(todo_setter=lambda t: self._set_todos(t))
        self.tools = tools
        self.system_prompt = build_system_prompt(base=system_prompt, extras=extras)
        self.reasoning_language = reasoning_language
        self.max_iterations = max_iterations
        self.temperature = temperature

        self.history: list[Message] = [Message(role=Role.SYSTEM, content=self.system_prompt)]
        self._lock = threading.Lock()
        # 由 ``_one_turn`` 写、``run`` 读的"本轮是否结束"信号；见 ``_one_turn`` 的 docstring。
        self._last_turn_stop: bool = False

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def todos(self) -> list[dict[str, Any]]:
        return list(self._todos)

    def reset(self) -> None:
        """Drop the conversation history but keep the system prompt."""
        with self._lock:
            self.history = [Message(role=Role.SYSTEM, content=self.system_prompt)]
            self._todos = []

    def run(self, user_input: str) -> Iterator[AgentEvent]:
        """Send a user message and drive the tool-loop until the model stops calling tools.

        Yields :class:`AgentEvent` items. Caller decides how to render.
        """
        # 1. user 回合：注入 reasoning-language 块（如有）
        composed = self._compose_user_turn(user_input)
        with self._lock:
            self.history.append(Message(role=Role.USER, content=composed))

        # 2. 主循环：最多 max_iterations 轮
        # ``_one_turn`` 通过 ``self._last_turn_stop`` 报告本轮是否结束（assistant 没再要工具）。
        for iteration in range(self.max_iterations):
            self._last_turn_stop = False
            try:
                yield from self._one_turn()
            except StreamInterruptedError as exc:
                yield AgentEvent(kind="error", error=exc, extra={"recoverable": True})
                return
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("agent: unexpected error")
                yield AgentEvent(kind="error", error=exc)
                return

            if self._last_turn_stop:
                yield AgentEvent(kind="turn_end", extra={"iterations": iteration + 1})
                return

        yield AgentEvent(
            kind="error",
            error=RuntimeError(f"max_iterations ({self.max_iterations}) reached"),
        )

    # ------------------------------------------------------------------
    # one model turn: stream → collect → tool exec → append
    # ------------------------------------------------------------------

    def _one_turn(self) -> Iterator[AgentEvent]:
        """Run a single model turn.

        本方法是个 generator。它通过 ``self._last_turn_stop`` 报告"本轮 assistant 是否
        没再请求工具"——为 ``True`` 时外层主循环就应当退出。这种用属性传信号的写法是
        因为 ``yield from`` 的返回值（StopIteration.value）会被静默吞掉，不可靠。
        """
        req = Request(
            messages=list(self.history),
            tools=self.tools.schemas(),
            temperature=self.temperature,
        )

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        completed_calls: list[ToolCall] = []
        seen_call_ids: set[str] = set()

        for chunk in self.provider.stream(req):
            if chunk.type == ChunkType.TEXT:
                text_parts.append(chunk.text)
                yield AgentEvent(kind="text", text=chunk.text)
            elif chunk.type == ChunkType.REASONING:
                reasoning_parts.append(chunk.text)
                yield AgentEvent(kind="reasoning", text=chunk.text)
            elif chunk.type == ChunkType.TOOL_CALL_START:
                # 仅做信号；不持久化
                if chunk.tool_call:
                    yield AgentEvent(
                        kind="tool_call",
                        tool_call=chunk.tool_call,
                        extra={"phase": "start"},
                    )
            elif chunk.type == ChunkType.TOOL_CALL:
                if chunk.tool_call and chunk.tool_call.id not in seen_call_ids:
                    seen_call_ids.add(chunk.tool_call.id)
                    completed_calls.append(chunk.tool_call)
            elif chunk.type == ChunkType.USAGE:
                if chunk.usage is not None:
                    yield AgentEvent(
                        kind="usage",
                        extra={
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                            "total_tokens": chunk.usage.total_tokens,
                        },
                    )
            elif chunk.type == ChunkType.ERROR:
                yield AgentEvent(kind="error", error=chunk.error)
                self._last_turn_stop = True
                return

        full_text = "".join(text_parts)
        full_reasoning = "".join(reasoning_parts)

        # 把 assistant 消息追加到历史
        assistant_msg = Message(
            role=Role.ASSISTANT,
            content=full_text,
            tool_calls=list(completed_calls),
            reasoning_content=full_reasoning,
        )
        with self._lock:
            self.history.append(assistant_msg)

        # 如果没有 tool_calls：本回合结束
        if not completed_calls:
            self._last_turn_stop = True
            return

        # 否则执行所有 tool_calls，追加 tool 消息
        results = self._run_tools(completed_calls)
        with self._lock:
            for call, res in zip(completed_calls, results):
                self.history.append(
                    Message(
                        role=Role.TOOL,
                        content=res.output,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
        for call, res in zip(completed_calls, results):
            yield AgentEvent(kind="tool_result", tool_call=call, tool_result=res)

        # 工具执行完 → 继续下一轮（``_last_turn_stop`` 已默认 False）

    # ------------------------------------------------------------------
    # tool execution (parallel iff all calls in batch are read-only)
    # ------------------------------------------------------------------

    def _run_tools(self, calls: list[ToolCall]) -> list[ToolResult]:
        """Execute a batch of tool calls.

        约束（与 Go 版一致）：
        - 全部 ``read_only=True`` → 并行（线程池）
        - 否则 → 严格串行，按 ``calls`` 顺序，保留写入顺序
        """
        if not calls:
            return []

        all_read_only = all(self._is_read_only(c.name) for c in calls)

        if all_read_only and len(calls) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(calls))) as pool:
                return list(pool.map(self._exec_one, calls))

        return [self._exec_one(c) for c in calls]

    def _is_read_only(self, name: str) -> bool:
        t = self.tools.get(name)
        return bool(t and t.read_only)

    def _exec_one(self, call: ToolCall) -> ToolResult:
        t = self.tools.get(call.name)
        if t is None:
            return ToolResult(
                output=f"unknown tool: {call.name}",
                is_error=True,
            )
        try:
            args = json.loads(call.arguments or "{}")
            if not isinstance(args, dict):
                return ToolResult(output="arguments must be a JSON object", is_error=True)
        except json.JSONDecodeError as exc:
            return ToolResult(output=f"invalid arguments JSON: {exc}", is_error=True)
        try:
            return t.execute(args)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("tool %s raised", call.name)
            return ToolResult(output=f"tool {call.name} crashed: {exc!r}", is_error=True)

    # ------------------------------------------------------------------
    # user-turn composer (reasoning-language block goes here, not in system slot)
    # ------------------------------------------------------------------

    def _compose_user_turn(self, user_input: str) -> str:
        block = reasoning_language_block(self.reasoning_language)
        if block:
            return block + "\n\n" + user_input
        return user_input

    def _set_todos(self, todos: list[dict[str, Any]]) -> None:
        self._todos = list(todos)
