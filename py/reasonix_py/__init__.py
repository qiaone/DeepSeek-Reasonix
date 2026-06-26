"""Reasonix-Py: minimal Python port of the Reasonix coding-agent kernel.

公开 API 集中在这里，便于 ``from reasonix_py import Agent, Registry, ...``。
"""

from .prompts import (
    DEFAULT_SYSTEM_PROMPT,
    USER_DECISION_POLICY,
    LANGUAGE_POLICY,
    reasoning_language_block,
)
from .messages import Role, Message, ToolCall, normalize_messages
from .provider import (
    Provider,
    Request,
    Chunk,
    ChunkType,
    OpenAICompatProvider,
    StreamInterruptedError,
)
from .tools import Tool, Registry, schema, tool
from .builtins import default_registry
from .agent import Agent, build_system_prompt

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "USER_DECISION_POLICY",
    "LANGUAGE_POLICY",
    "reasoning_language_block",
    "Role",
    "Message",
    "ToolCall",
    "normalize_messages",
    "Provider",
    "Request",
    "Chunk",
    "ChunkType",
    "OpenAICompatProvider",
    "StreamInterruptedError",
    "Tool",
    "Registry",
    "schema",
    "tool",
    "default_registry",
    "Agent",
    "build_system_prompt",
    "create_app",
]


def create_app(*args, **kwargs):
    """Lazy import of :func:`reasonix_py.serve.create_app` (FastAPI optional)."""
    from .serve import create_app as _create_app

    return _create_app(*args, **kwargs)

__version__ = "0.0.1"
