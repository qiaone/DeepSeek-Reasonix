"""Tool abstraction + Registry, mirroring Go ``internal/tool/tool.go``.

设计要点：
- ``Tool`` 抽象只要求 4 件事：name / description / JSON Schema / execute。
- ``read_only`` 决定一批 tool_calls 能否并行执行（agent 主循环按此判定）。
- ``Registry`` 既支持代码注册，也支持 ``@tool`` 装饰器（基于函数自动构 schema）。
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional


@dataclass
class ToolResult:
    """工具执行结果。``output`` 会作为 ``tool`` 消息的 ``content`` 回灌给模型。"""

    output: str
    is_error: bool = False


class Tool:
    """A capability the model can invoke.

    子类需要实现 4 个属性/方法：``name`` / ``description`` / ``schema`` / ``execute``。
    ``read_only`` 默认 ``False``（保守：未明确声明就当作有副作用）。
    """

    name: str = ""
    description: str = ""
    read_only: bool = False

    @property
    def schema(self) -> dict[str, Any]:  # pragma: no cover - abstract
        """JSON Schema for this tool's parameters (the ``parameters`` field of OpenAI ``tools``)."""
        raise NotImplementedError

    def execute(self, args: dict[str, Any]) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError

    # -------- 内部辅助 --------

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI Chat Completions ``tools`` 数组里的一项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class Registry:
    """Per-run set of tools — built-ins + plugin/MCP tools.

    保留首次插入的顺序，按名字唯一去重。``schemas()`` 给 provider 用，``get()`` 给 agent 执行用。
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._order: list[str] = []

    def add(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool.name is required")
        if tool.name not in self._tools:
            self._order.append(tool.name)
        self._tools[tool.name] = tool

    def extend(self, tools: Iterable[Tool]) -> None:
        for t in tools:
            self.add(t)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._order)

    def names(self) -> list[str]:
        return list(self._order)

    def schemas(self) -> list[dict[str, Any]]:
        return [self._tools[n].to_openai_schema() for n in self._order]


# ---------------------------------------------------------------------------
# Function-based tool helpers
# ---------------------------------------------------------------------------


def schema(
    *,
    type: str = "object",
    properties: Optional[dict[str, Any]] = None,
    required: Optional[list[str]] = None,
    additional_properties: bool = False,
) -> dict[str, Any]:
    """Build a minimal JSON Schema dict (``object`` is by far the most common)."""
    out: dict[str, Any] = {"type": type}
    if properties is not None:
        out["properties"] = properties
    if required:
        out["required"] = required
    if type == "object":
        out["additionalProperties"] = additional_properties
    return out


class _FunctionTool(Tool):
    """Adapter that wraps a Python function as a Tool."""

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        read_only: bool,
    ) -> None:
        self._fn = fn
        self.name = name
        self.description = description
        self._params = parameters
        self.read_only = read_only

    @property
    def schema(self) -> dict[str, Any]:
        return self._params

    def execute(self, args: dict[str, Any]) -> ToolResult:
        try:
            result = self._fn(**(args or {}))
        except TypeError as exc:
            return ToolResult(output=f"bad arguments: {exc}", is_error=True)
        except Exception as exc:  # pragma: no cover - defensive
            return ToolResult(output=f"tool error: {exc!r}", is_error=True)

        if isinstance(result, ToolResult):
            return result
        if isinstance(result, str):
            return ToolResult(output=result)
        try:
            return ToolResult(output=json.dumps(result, ensure_ascii=False, indent=2))
        except (TypeError, ValueError):
            return ToolResult(output=str(result))


def tool(
    *,
    name: Optional[str] = None,
    description: str = "",
    parameters: Optional[dict[str, Any]] = None,
    read_only: bool = False,
) -> Callable[[Callable[..., Any]], Tool]:
    """Decorator: turn a plain function into a :class:`Tool`.

    Example
    -------
    >>> @tool(description="Echo input.", parameters=schema(properties={"text": {"type": "string"}}, required=["text"]))
    ... def echo(text: str) -> str:
    ...     return text
    """

    def decorator(fn: Callable[..., Any]) -> Tool:
        nm = name or fn.__name__
        desc = description or (inspect.getdoc(fn) or "").strip()
        params = parameters or schema()
        return _FunctionTool(
            fn,
            name=nm,
            description=desc,
            parameters=params,
            read_only=read_only,
        )

    return decorator
