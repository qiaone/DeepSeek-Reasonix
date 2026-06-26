"""Built-in tools — minimal but complete enough for a usable coding agent.

只复刻了与 Go 内核同名的最核心 5 个：

- ``read_file`` — 按行号窗口读取文件
- ``write_file`` — 全文写入（覆盖；缺失目录自动创建）
- ``ls`` — 列目录
- ``bash`` — 跨平台 shell 命令（Windows 下走 PowerShell，否则 ``/bin/sh``）
- ``todo_write`` — DefaultSystemPrompt 里要求的多步骤 todo 跟踪

更多工具（grep / glob / edit_file / multi_replace / web_fetch / ask）可以参考
``internal/tool/builtin/`` 在此处补全。
"""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from .tools import Registry, Tool, ToolResult, schema


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a UTF-8 text file from disk. Returns the file content with line "
        "numbers prepended. Use ``offset`` (1-based) and ``limit`` to read a window."
    )
    read_only = True

    @property
    def schema(self) -> dict[str, Any]:
        return schema(
            properties={
                "path": {"type": "string", "description": "Absolute or relative file path."},
                "offset": {
                    "type": "integer",
                    "description": "1-based line offset; default 1.",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return; default 2000.",
                    "minimum": 1,
                },
            },
            required=["path"],
        )

    def execute(self, args: dict[str, Any]) -> ToolResult:
        p = Path(str(args.get("path", ""))).expanduser()
        if not p.exists():
            return ToolResult(output=f"file not found: {p}", is_error=True)
        if p.is_dir():
            return ToolResult(output=f"path is a directory: {p}", is_error=True)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(output=f"read error: {exc}", is_error=True)

        lines = text.splitlines()
        offset = max(1, int(args.get("offset", 1) or 1))
        limit = max(1, int(args.get("limit", 2000) or 2000))
        start = offset - 1
        end = min(len(lines), start + limit)
        slice_ = lines[start:end]
        # 加行号方便模型引用
        numbered = "\n".join(f"{start + i + 1:>6}\t{line}" for i, line in enumerate(slice_))
        header = (
            f"# {p}  ({len(lines)} lines total; showing {start + 1}-{end})\n"
        )
        return ToolResult(output=header + numbered)


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write text content to a file (overwriting). Creates parent directories "
        "if needed. Always pass the full intended file content."
    )
    read_only = False

    @property
    def schema(self) -> dict[str, Any]:
        return schema(
            properties={
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            required=["path", "content"],
        )

    def execute(self, args: dict[str, Any]) -> ToolResult:
        p = Path(str(args.get("path", ""))).expanduser()
        content = str(args.get("content", ""))
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(output=f"write error: {exc}", is_error=True)
        return ToolResult(output=f"wrote {len(content)} bytes to {p}")


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


class LsTool(Tool):
    name = "ls"
    description = "List the entries of a directory (one per line, with type indicator)."
    read_only = True

    @property
    def schema(self) -> dict[str, Any]:
        return schema(
            properties={
                "path": {"type": "string", "description": "Directory; default '.'"},
            },
        )

    def execute(self, args: dict[str, Any]) -> ToolResult:
        p = Path(str(args.get("path", ".") or ".")).expanduser()
        if not p.exists():
            return ToolResult(output=f"path not found: {p}", is_error=True)
        if not p.is_dir():
            return ToolResult(output=f"not a directory: {p}", is_error=True)
        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except OSError as exc:
            return ToolResult(output=f"ls error: {exc}", is_error=True)
        out_lines = [f"# {p}  ({len(entries)} entries)"]
        for e in entries:
            kind = "DIR " if e.is_dir() else "FILE"
            out_lines.append(f"{kind}\t{e.name}")
        return ToolResult(output="\n".join(out_lines))


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------


class BashTool(Tool):
    name = "bash"
    description = (
        "Run a shell command. On Windows the command runs through ``powershell -NoProfile -Command``; "
        "elsewhere through ``/bin/sh -c``. Captures stdout+stderr; truncates output above ``max_output_bytes``."
    )
    read_only = False  # Bash 在 Go 版同样硬编码 ReadOnly=false（无法静态推断副作用）

    DEFAULT_TIMEOUT = 120
    DEFAULT_MAX_OUTPUT = 100_000  # 字节，超过会截断

    @property
    def schema(self) -> dict[str, Any]:
        return schema(
            properties={
                "command": {"type": "string", "description": "Shell command line."},
                "cwd": {"type": "string", "description": "Working directory; default cwd."},
                "timeout_seconds": {
                    "type": "integer",
                    "description": f"Hard timeout; default {self.DEFAULT_TIMEOUT}.",
                    "minimum": 1,
                },
            },
            required=["command"],
        )

    def execute(self, args: dict[str, Any]) -> ToolResult:
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return ToolResult(output="empty command", is_error=True)
        cwd = args.get("cwd") or None
        if cwd:
            cwd = str(Path(cwd).expanduser())

        timeout = int(args.get("timeout_seconds", self.DEFAULT_TIMEOUT) or self.DEFAULT_TIMEOUT)

        if platform.system() == "Windows":
            argv = ["powershell", "-NoProfile", "-Command", cmd]
            shell = False
        else:
            argv = ["/bin/sh", "-c", cmd]
            shell = False

        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                shell=shell,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or "") + (exc.stderr or "")
            return ToolResult(
                output=f"[timeout after {timeout}s]\n{_truncate(partial, self.DEFAULT_MAX_OUTPUT)}",
                is_error=True,
            )
        except FileNotFoundError as exc:
            return ToolResult(output=f"shell not available: {exc}", is_error=True)

        combined = (proc.stdout or "") + (proc.stderr or "")
        combined = _truncate(combined, self.DEFAULT_MAX_OUTPUT)
        header = f"[exit={proc.returncode}]\n"
        return ToolResult(output=header + combined, is_error=proc.returncode != 0)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n... [truncated {len(s) - n} bytes]"


# ---------------------------------------------------------------------------
# todo_write
# ---------------------------------------------------------------------------


class TodoWriteTool(Tool):
    """In-memory task list per agent run.

    与 Go 版 ``todo_write`` 的契约对齐：每次 **完整覆盖**整个列表，并要求恰好一项处于
    ``in_progress``。这里把状态存在 agent 上（``agent._todos``），所以本工具持有
    一个回写回调 ``setter``。
    """

    name = "todo_write"
    description = (
        "Replace the agent's todo list. Pass the FULL desired list every call. "
        "Exactly one item must be ``in_progress``. Status values: ``pending`` | ``in_progress`` | ``completed``."
    )
    read_only = True  # 不动磁盘；只动 agent 内存

    def __init__(self, setter):
        self._setter = setter

    @property
    def schema(self) -> dict[str, Any]:
        return schema(
            properties={
                "todos": {
                    "type": "array",
                    "items": schema(
                        properties={
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        required=["content", "status"],
                    ),
                }
            },
            required=["todos"],
        )

    def execute(self, args: dict[str, Any]) -> ToolResult:
        todos = args.get("todos") or []
        if not isinstance(todos, list):
            return ToolResult(output="todos must be a list", is_error=True)
        in_progress = [t for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"]
        if todos and len(in_progress) != 1 and any(t.get("status") != "completed" for t in todos):
            # 与 Go 版一致：仅在还有未完成项时强制 in_progress 唯一
            return ToolResult(
                output=(
                    "exactly one todo must be in_progress while there are unfinished items "
                    f"(got {len(in_progress)})"
                ),
                is_error=True,
            )
        self._setter(todos)
        return ToolResult(output=_format_todos(todos))


def _format_todos(todos: list[dict[str, Any]]) -> str:
    if not todos:
        return "(no todos)"
    out = ["# todos"]
    icon = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    for t in todos:
        st = str(t.get("status", "pending"))
        out.append(f"{icon.get(st, '[?]')} {t.get('content', '')}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# default registry
# ---------------------------------------------------------------------------


def default_registry(*, todo_setter: Optional[callable] = None) -> Registry:
    """Build a :class:`Registry` populated with the 5 default built-in tools.

    Parameters
    ----------
    todo_setter:
        ``Agent`` 在构造时会传入一个 ``lambda todos: setattr(self, "_todos", todos)``。
        独立调用时可省略，``todo_write`` 仍可执行（只是结果不被外部观察）。
    """
    reg = Registry()
    reg.add(ReadFileTool())
    reg.add(WriteFileTool())
    reg.add(LsTool())
    reg.add(BashTool())
    reg.add(TodoWriteTool(setter=todo_setter or (lambda _todos: None)))
    return reg
