"""Conversation message types and history normalization.

对应 Go ``internal/provider`` 里 ``Message`` / ``ToolCall`` 与 ``NormalizeMessages``。
这里只保留最关键的不变量：每个 ``assistant`` 带 ``tool_calls`` 的回合后必须紧跟
对应数量的 ``tool`` 回复（按 ``tool_call_id`` 配对），缺失的会用占位结果回填，
否则 OpenAI/DeepSeek 会以 HTTP 400 拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# 与 Go 版一致：未完成的 tool 调用占位结果。
INTERRUPTED_TOOL_RESULT = (
    "[no result: the previous turn was interrupted before this tool call completed]"
)


class Role:
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    """A single tool invocation requested by the model.

    ``arguments`` 永远是**原始 JSON 字符串**，不是已解析的 dict —— 与 Go 版 ``ToolCall.Arguments``
    保持一致。这样既方便流式增量拼接，也便于把"半截 JSON"修复后再 round-trip。
    """

    id: str
    name: str
    arguments: str = ""

    def to_wire(self) -> dict[str, Any]:
        """转成 OpenAI Chat Completions 请求里的 tool_calls 项。"""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments or "{}"},
        }


@dataclass
class Message:
    """One conversation message.

    Parameters
    ----------
    role:
        ``Role.SYSTEM | USER | ASSISTANT | TOOL``。
    content:
        文本内容。``assistant`` 仅有 tool_calls 时可以为空字符串。
    tool_calls:
        仅 ``assistant`` 有意义。
    tool_call_id:
        仅 ``tool`` 角色有意义，对应 ``ToolCall.id``。
    name:
        ``tool`` 角色：被调用的工具名（OpenAI Chat Completions API 需要）。
    reasoning_content:
        thinking-mode 的可见推理文本，多轮回放时需要原样回填（Anthropic 强制要求）。
    """

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""
    reasoning_content: str = ""

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role}
        # OpenAI 要求 content 至少存在；assistant 在只 tool_call 时可以为 ``""``。
        if self.role == Role.TOOL:
            out["content"] = self.content
            if self.tool_call_id:
                out["tool_call_id"] = self.tool_call_id
            if self.name:
                out["name"] = self.name
            return out

        if self.role == Role.ASSISTANT and self.tool_calls:
            out["content"] = self.content or ""
            out["tool_calls"] = [tc.to_wire() for tc in self.tool_calls]
            return out

        out["content"] = self.content or ""
        return out


def normalize_messages(msgs: Iterable[Message]) -> list[Message]:
    """Repair history to satisfy the OpenAI tool-call pairing contract.

    保持的不变量：
    1. 每个 ``assistant`` 带 ``tool_calls`` 的消息后必须紧跟 N 个 ``tool`` 消息（N == len(tool_calls)），
       且每个 ``tool`` 消息的 ``tool_call_id`` 与对应 call.id 匹配。
    2. 孤儿 ``tool`` 消息（前面没有对应的 ``tool_calls``）被丢弃。
    3. 缺失的 ``tool`` 回复用 ``INTERRUPTED_TOOL_RESULT`` 占位回填。
    4. 半截的 ``arguments`` JSON 被简单修复为 ``"{}"``（避免 DeepSeek 400）。

    这是 Go ``provider.NormalizeMessages`` 的**简化版**：只保留 wire-safe 这部分修复。
    """
    msgs = list(msgs)
    out: list[Message] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.role == Role.ASSISTANT and m.tool_calls:
            # 收集后续连续的 tool 消息
            j = i + 1
            tool_msgs: list[Message] = []
            while j < len(msgs) and msgs[j].role == Role.TOOL:
                tool_msgs.append(msgs[j])
                j += 1

            # 修复 arguments
            fixed_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.name,
                    arguments=_repair_args(tc.arguments),
                )
                for tc in m.tool_calls
            ]
            out.append(
                Message(
                    role=Role.ASSISTANT,
                    content=m.content,
                    tool_calls=fixed_calls,
                    reasoning_content=m.reasoning_content,
                )
            )

            # 按 call.id 配对，缺的用占位回填
            by_id = {t.tool_call_id: t for t in tool_msgs if t.tool_call_id}
            for call in fixed_calls:
                if call.id in by_id:
                    t = by_id[call.id]
                    out.append(
                        Message(
                            role=Role.TOOL,
                            content=t.content or INTERRUPTED_TOOL_RESULT,
                            tool_call_id=call.id,
                            name=t.name or call.name,
                        )
                    )
                else:
                    out.append(
                        Message(
                            role=Role.TOOL,
                            content=INTERRUPTED_TOOL_RESULT,
                            tool_call_id=call.id,
                            name=call.name,
                        )
                    )
            i = j
            continue

        if m.role == Role.TOOL:
            # 孤儿 tool 消息，丢弃
            i += 1
            continue

        out.append(m)
        i += 1
    return out


def _repair_args(args: str) -> str:
    """Best-effort 修复半截 arguments JSON。

    Go 版有更细的 ``repairToolCallArgs``；Python 这里只做：空 → ``"{}"``，无法解析 → ``"{}"``。
    """
    if not args or not args.strip():
        return "{}"
    import json

    try:
        json.loads(args)
        return args
    except Exception:
        return "{}"
