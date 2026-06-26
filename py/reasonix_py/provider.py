"""Provider abstraction + OpenAI-compatible streaming implementation.

对应 Go ``internal/provider/provider.go`` 的 ``Provider`` / ``Request`` / ``Chunk`` 与
``internal/provider/openai/openai.go`` 的具体实现。设计要点：

- ``Provider.stream`` 返回一个 **生成器 / 迭代器**，按事件粒度产出 ``Chunk``。
- ``Chunk`` 携带 5 类事件：text / reasoning / tool_call_start / tool_call / usage / error。
- OpenAI / DeepSeek / 任何兼容 ``/chat/completions`` SSE 的端点都能用 ``OpenAICompatProvider``。
- 流被中途断开时，抛 ``StreamInterruptedError`` —— 调用者（agent）应在末尾追加一段
  recovery 提示再续，而不是重发请求（避免重复输出）。
"""

from __future__ import annotations

import enum
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import httpx

from .messages import Message, ToolCall, normalize_messages


class ChunkType(enum.Enum):
    TEXT = "text"               # 可见文本增量
    REASONING = "reasoning"     # thinking-mode 推理增量（在最终答案之前）
    TOOL_CALL_START = "tool_call_start"  # 一个 tool_call 开始（仅 id+name 已知）
    TOOL_CALL = "tool_call"     # 一个完整的 tool_call（args 已收齐）
    USAGE = "usage"             # token 用量
    ERROR = "error"             # 出错


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class Chunk:
    type: ChunkType
    text: str = ""
    signature: str = ""
    tool_call: Optional[ToolCall] = None
    usage: Optional[Usage] = None
    error: Optional[BaseException] = None


@dataclass
class Request:
    """Single completion request."""

    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)  # 已是 OpenAI ``tools`` 格式
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)


class StreamInterruptedError(Exception):
    """Raised when the SSE stream is cut after some output was already produced."""


class Provider:
    """Abstract base class for chat-capable model backends."""

    name: str = "provider"

    def stream(self, req: Request) -> Iterator[Chunk]:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OpenAI-compatible streaming implementation.
# ---------------------------------------------------------------------------


class OpenAICompatProvider(Provider):
    """Streaming chat-completions client for OpenAI / DeepSeek / compatible endpoints.

    Parameters
    ----------
    base_url:
        例如 ``"https://api.deepseek.com/v1"`` 或 ``"https://api.openai.com/v1"``。
    api_key:
        Bearer token。
    model:
        例如 ``"deepseek-chat"`` / ``"gpt-4o-mini"``。
    name:
        provider 实例名（仅用于日志/调试）。
    timeout:
        ``httpx`` 请求超时（秒）。流式连接长，建议 ``None`` 或较大值。
    extra_headers:
        额外的 HTTP 头（如 Anthropic 的 ``anthropic-version``）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        name: str = "openai-compat",
        timeout: Optional[float] = 600.0,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = name
        self.timeout = timeout
        self.extra_headers = extra_headers or {}

    @classmethod
    def from_env(cls) -> "OpenAICompatProvider":
        """从环境变量构造。

        识别（按优先级）：
        - ``REASONIX_BASE_URL`` / ``REASONIX_API_KEY`` / ``REASONIX_MODEL``
        - 否则尝试 ``DEEPSEEK_API_KEY`` + ``https://api.deepseek.com/v1`` + ``deepseek-chat``
        - 否则尝试 ``OPENAI_API_KEY`` + ``https://api.openai.com/v1`` + ``gpt-4o-mini``
        """
        base_url = os.getenv("REASONIX_BASE_URL", "").strip()
        api_key = os.getenv("REASONIX_API_KEY", "").strip()
        model = os.getenv("REASONIX_MODEL", "").strip()

        if not api_key:
            if os.getenv("DEEPSEEK_API_KEY"):
                api_key = os.environ["DEEPSEEK_API_KEY"]
                base_url = base_url or "https://api.deepseek.com/v1"
                model = model or "deepseek-chat"
            elif os.getenv("OPENAI_API_KEY"):
                api_key = os.environ["OPENAI_API_KEY"]
                base_url = base_url or "https://api.openai.com/v1"
                model = model or "gpt-4o-mini"

        if not api_key:
            raise RuntimeError(
                "OpenAICompatProvider.from_env: no API key found. "
                "Set REASONIX_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY."
            )
        if not base_url:
            raise RuntimeError("OpenAICompatProvider.from_env: REASONIX_BASE_URL not set.")
        if not model:
            raise RuntimeError("OpenAICompatProvider.from_env: REASONIX_MODEL not set.")
        return cls(base_url=base_url, api_key=api_key, model=model)

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------

    def stream(self, req: Request) -> Iterator[Chunk]:
        msgs = normalize_messages(req.messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": True,
            "messages": [m.to_wire() for m in msgs],
            "temperature": req.temperature,
        }
        if req.max_tokens:
            payload["max_tokens"] = req.max_tokens
        if req.tools:
            payload["tools"] = req.tools
            payload["tool_choice"] = "auto"
        # extra 允许调用方覆盖任何字段（如 ``response_format``）
        for k, v in req.extra.items():
            payload[k] = v

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        headers.update(self.extra_headers)

        url = f"{self.base_url}/chat/completions"
        produced_any = False
        # 用 ``stream`` 上下文逐行读取 SSE
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code in (401, 403):
                        body = resp.read().decode("utf-8", errors="replace")
                        raise RuntimeError(
                            f"authentication failed (HTTP {resp.status_code}): {body[:300]}"
                        )
                    if resp.status_code >= 400:
                        body = resp.read().decode("utf-8", errors="replace")
                        raise RuntimeError(
                            f"provider returned HTTP {resp.status_code}: {body[:500]}"
                        )

                    yield from self._iter_sse(resp)
                    produced_any = True
        except httpx.RemoteProtocolError as exc:
            if produced_any:
                raise StreamInterruptedError(str(exc)) from exc
            raise
        except httpx.ReadError as exc:
            if produced_any:
                raise StreamInterruptedError(str(exc)) from exc
            raise

    # ------------------------------------------------------------------
    # SSE parser — 维护一个"当前正在累积"的 tool_calls 表，按 ``index`` 索引。
    # OpenAI 把 ``arguments`` 切成多片增量发送，我们要拼起来再产出 ChunkToolCall。
    # ------------------------------------------------------------------

    def _iter_sse(self, resp: httpx.Response) -> Iterator[Chunk]:
        # tool_calls 累积器：index -> ToolCall
        tcs: dict[int, ToolCall] = {}
        tcs_started: set[int] = set()

        for line in resp.iter_lines():
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                # 把所有累积完的 tool_call 输出
                for idx in sorted(tcs):
                    yield Chunk(type=ChunkType.TOOL_CALL, tool_call=tcs[idx])
                return
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue

            choices = obj.get("choices") or []
            if not choices:
                # 末尾的 usage 帧可能没有 choices
                if "usage" in obj and obj["usage"]:
                    u = obj["usage"]
                    yield Chunk(
                        type=ChunkType.USAGE,
                        usage=Usage(
                            prompt_tokens=u.get("prompt_tokens", 0),
                            completion_tokens=u.get("completion_tokens", 0),
                            total_tokens=u.get("total_tokens", 0),
                        ),
                    )
                continue

            ch0 = choices[0]
            delta = ch0.get("delta") or {}

            if "reasoning_content" in delta and delta["reasoning_content"]:
                yield Chunk(type=ChunkType.REASONING, text=delta["reasoning_content"])

            if "content" in delta and delta["content"]:
                yield Chunk(type=ChunkType.TEXT, text=delta["content"])

            for tc_delta in delta.get("tool_calls") or []:
                idx = tc_delta.get("index", 0)
                cur = tcs.get(idx) or ToolCall(id="", name="", arguments="")
                if tc_delta.get("id"):
                    cur.id = tc_delta["id"]
                fn = tc_delta.get("function") or {}
                if fn.get("name"):
                    cur.name = fn["name"]
                if "arguments" in fn and fn["arguments"]:
                    cur.arguments += fn["arguments"]
                tcs[idx] = cur
                # 第一次见到这个 index 且 id+name 已知时，emit start 事件
                if idx not in tcs_started and cur.id and cur.name:
                    tcs_started.add(idx)
                    yield Chunk(
                        type=ChunkType.TOOL_CALL_START,
                        tool_call=ToolCall(id=cur.id, name=cur.name, arguments=""),
                    )

            if ch0.get("finish_reason"):
                # finish 时把累积的 tool_calls 全部 emit
                for idx in sorted(tcs):
                    yield Chunk(type=ChunkType.TOOL_CALL, tool_call=tcs[idx])
                tcs.clear()
                tcs_started.clear()

            if "usage" in obj and obj["usage"]:
                u = obj["usage"]
                yield Chunk(
                    type=ChunkType.USAGE,
                    usage=Usage(
                        prompt_tokens=u.get("prompt_tokens", 0),
                        completion_tokens=u.get("completion_tokens", 0),
                        total_tokens=u.get("total_tokens", 0),
                    ),
                )

        # 流自然结束但没有 [DONE]：把累积的 emit 一次
        for idx in sorted(tcs):
            yield Chunk(type=ChunkType.TOOL_CALL, tool_call=tcs[idx])
