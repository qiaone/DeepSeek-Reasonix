"""FastAPI + uvicorn web frontend for reasonix_py.

设计目标
========

复刻 Go 端 ``internal/serve`` 中**前端 ``index.html`` 真正会调用**的端点子集，
让那份现成的 HTML 单页应用可以直接驱动 Python agent。

复刻过的端点（前端真正使用）
----------------------------

GET 端点：

- ``/``               返回 index.html（路径可通过 ``--index`` 覆盖）
- ``/events``         Server-Sent Events，主推送通道
- ``/history``        会话历史（OpenAI 风格 wire-shape）
- ``/context``        上下文使用量（``{used, window}``）
- ``/status``         运行状态、模型标签、模式开关
- ``/todos``          当前 todo 列表
- ``/sessions``       会话列表（**stub**：永远 ``[]``）
- ``/skills``         技能列表（**stub**：永远 ``[]``）
- ``/checkpoints``    检查点列表（**stub**：永远 ``[]``）
- ``/branches``       分支列表（**stub**：永远 ``[]``）

POST 端点：

- ``/submit``         提交一条用户输入；启动 turn
- ``/cancel``         请求取消当前 turn
- ``/new``            清空历史，开新会话
- ``/plan``           plan-mode 开关
- ``/tool-approval-mode``  ``ask`` / ``auto`` / ``yolo``
- ``/auto-approve-tools``  ``yolo`` 兼容别名
- ``/bypass``         同上（旧客户端兼容）
- ``/compact``        触发上下文压缩（**stub**：发 ``compaction_*`` 事件即可）
- ``/resume``、``/forget``、``/answer``、``/approve``、
  ``/goal``、``/rewind``、``/fork``、``/summarize``、``/delete-session``
  → **stub**：返回 204，避免前端报错

SSE 事件协议（kind 字段）
-------------------------

参考 ``internal/serve/index.html`` 的 ``es.onmessage`` 分发逻辑，
本文件把 :class:`reasonix_py.agent.AgentEvent` 翻译成下列 ``kind``：

============  ===========================================
``kind``       含义
============  ===========================================
turn_started   一次 user→assistant 回合开始
text           可见文本增量
reasoning      thinking 推理增量
message        断句标记（一段 assistant 输出结束）
tool_dispatch  工具开始
tool_result    工具完成（含 output / err）
usage          token 用量
turn_done      回合结束（带可选 err）
notice         普通通知文本
phase          阶段切换（plan / answer / ...）
retrying       provider 重试中
============  ===========================================

线程模型
========

FastAPI 跑在 asyncio 事件循环里，而 :meth:`Agent.run` 是**同步生成器**且
工具调用可能阻塞（比如 ``bash`` 子进程）。所以：

- 每个 ``/submit`` 启动一个 **worker 线程**，跑 agent 循环；
- worker 通过 :class:`Broadcaster` 把事件投递给 asyncio 端；
- ``/events`` 的每个连接是一个独立的 ``asyncio.Queue`` 订阅者；
- 一个进程同时只允许 **一个 active turn**（``running`` 标志），与 Go 端一致。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .agent import Agent, AgentEvent
from .config import load_dotenv
from .messages import Message, Role
from .provider import OpenAICompatProvider, Provider

# FastAPI 是可选依赖；为了让 ``from __future__ import annotations`` 下
# ``req: Request`` 这种参数注解能被 FastAPI 通过 ``get_type_hints`` 解析到，
# 这里在模块顶层导入 ``Request`` 名字（找不到时退化成 ``Any``，``create_app`` 里会再抛清晰错误）。
try:
    from fastapi import Request  # type: ignore
except ImportError:  # pragma: no cover - exercised only when fastapi is missing
    Request = Any  # type: ignore[assignment, misc]

log = logging.getLogger("reasonix_py.serve")


# ---------------------------------------------------------------------------
# Broadcaster — async fan-out for SSE subscribers.
# ---------------------------------------------------------------------------


class Broadcaster:
    """Fan-out queue: many SSE subscribers, one publisher (the agent worker).

    线程安全：``publish`` 从 worker 线程调用，内部用 ``loop.call_soon_threadsafe``
    把事件投递到主事件循环上的各个 ``asyncio.Queue``。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._subs: list[asyncio.Queue[dict[str, Any]]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish(self, evt: dict[str, Any]) -> None:
        """Thread-safe publish from any thread."""
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            self._loop.call_soon_threadsafe(self._safe_put, q, evt)

    @staticmethod
    def _safe_put(q: asyncio.Queue[dict[str, Any]], evt: dict[str, Any]) -> None:
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            # 慢消费者：丢一个最老的，再塞新的
            try:
                _ = q.get_nowait()
            except Exception:
                return
            try:
                q.put_nowait(evt)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Server state — wraps an Agent + the broadcaster + run flags.
# ---------------------------------------------------------------------------


@dataclass
class ServerState:
    agent: Agent
    broadcaster: Broadcaster
    plan_mode: bool = False
    tool_approval_mode: str = "ask"   # ask | auto | yolo
    cumulative_usage: dict[str, int] = field(
        default_factory=lambda: {
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "cacheHitTokens": 0,
            "cacheMissTokens": 0,
        }
    )
    last_usage: dict[str, Any] = field(default_factory=dict)

    # turn 控制
    running: bool = False
    cancel_flag: threading.Event = field(default_factory=threading.Event)
    worker: Optional[threading.Thread] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


# ---------------------------------------------------------------------------
# AgentEvent → SSE wire shape
# ---------------------------------------------------------------------------


def _agent_event_to_wire(ev: AgentEvent) -> Optional[dict[str, Any]]:
    """Translate one ``AgentEvent`` to the JSON shape index.html expects.

    ``None`` means "drop this event".
    """
    k = ev.kind

    if k == "text":
        return {"kind": "text", "text": ev.text}

    if k == "reasoning":
        return {"kind": "reasoning", "text": ev.text}

    if k == "tool_call":
        if ev.extra.get("phase") != "start" or ev.tool_call is None:
            return None
        # 注意：start 阶段 args 还没收齐，这里给空串；
        # ``tool_result`` 时再补 args（前端会合并显示）。
        return {
            "kind": "tool_dispatch",
            "tool": {
                "id": ev.tool_call.id,
                "name": ev.tool_call.name,
                "args": "",
                "readOnly": False,
            },
        }

    if k == "tool_result":
        if ev.tool_call is None or ev.tool_result is None:
            return None
        out = {
            "id": ev.tool_call.id,
            "name": ev.tool_call.name,
            "args": ev.tool_call.arguments or "",
            "output": ev.tool_result.output,
        }
        if ev.tool_result.is_error:
            out["err"] = ev.tool_result.output
        return {"kind": "tool_result", "tool": out}

    if k == "usage":
        u = {
            "promptTokens": ev.extra.get("prompt_tokens", 0),
            "completionTokens": ev.extra.get("completion_tokens", 0),
            "totalTokens": ev.extra.get("total_tokens", 0),
        }
        return {"kind": "usage", "usage": u}

    if k == "turn_end":
        return {"kind": "turn_done"}

    if k == "error":
        return {"kind": "turn_done", "err": repr(ev.error) if ev.error else "error"}

    return None


# ---------------------------------------------------------------------------
# History wire shape — matches Go ``historyMessage``.
# ---------------------------------------------------------------------------


def _history_wire(history: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in history:
        if m.role == Role.SYSTEM:
            continue  # 前端不显示
        item: dict[str, Any] = {"role": m.role, "content": m.content or ""}
        if m.reasoning_content:
            item["reasoning"] = m.reasoning_content
        if m.role == Role.ASSISTANT and m.tool_calls:
            item["toolCalls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments or ""}
                for tc in m.tool_calls
            ]
        if m.role == Role.TOOL:
            if m.tool_call_id:
                item["toolCallId"] = m.tool_call_id
            if m.name:
                item["toolName"] = m.name
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Worker that runs one turn on a background thread.
# ---------------------------------------------------------------------------


def _run_turn(state: ServerState, user_input: str) -> None:
    """Body of the worker thread launched by ``/submit``."""
    state.broadcaster.publish({"kind": "turn_started"})
    if state.plan_mode:
        state.broadcaster.publish({"kind": "phase", "text": "plan mode"})

    last_was_text_or_reason = False
    err_repr: Optional[str] = None
    try:
        for ev in state.agent.run(user_input):
            if state.cancel_flag.is_set():
                state.broadcaster.publish({"kind": "notice", "level": "warn", "text": "cancelled"})
                err_repr = "cancelled"
                break

            wire = _agent_event_to_wire(ev)
            if wire is None:
                continue
            kind = wire["kind"]

            # 把"段落"边界发给前端
            if kind in {"text", "reasoning"}:
                last_was_text_or_reason = True
            elif kind in {"tool_dispatch", "tool_result"}:
                if last_was_text_or_reason:
                    state.broadcaster.publish({"kind": "message"})
                    last_was_text_or_reason = False

            # 累计 usage
            if kind == "usage":
                u = wire.get("usage") or {}
                state.last_usage = dict(u)
                for k_ in ("promptTokens", "completionTokens", "totalTokens"):
                    state.cumulative_usage[k_] += int(u.get(k_, 0) or 0)

            if kind == "turn_done" and "err" in wire:
                err_repr = wire["err"]

            state.broadcaster.publish(wire)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("worker: unhandled error")
        err_repr = repr(exc)

    finally:
        # 不管是否提前退出，都发一条 turn_done 收尾（前端用它清 running）
        evt: dict[str, Any] = {"kind": "turn_done"}
        if err_repr:
            evt["err"] = err_repr
        state.broadcaster.publish(evt)
        with state.lock:
            state.running = False
            state.cancel_flag.clear()
            state.worker = None


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    agent: Agent,
    index_html_path: Path,
    cors_origin: Optional[str] = None,
) -> "FastAPI":  # noqa: F821 - forward ref (FastAPI imported below)
    """Build the FastAPI ``app`` bound to the given :class:`Agent`."""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import (
            FileResponse,
            JSONResponse,
            PlainTextResponse,
            Response,
            StreamingResponse,
        )
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "fastapi is required for reasonix_py.serve; install with "
            "`pip install reasonix-py[serve]` or `pip install fastapi uvicorn[standard]`."
        ) from exc

    if cors_origin:
        from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="reasonix-py", docs_url=None, redoc_url=None)

    if cors_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[cors_origin],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization"],
        )

    # Broadcaster 在 startup 时初始化（要拿到当前事件循环）
    state_holder: dict[str, ServerState] = {}

    @app.on_event("startup")
    async def _on_startup() -> None:
        loop = asyncio.get_running_loop()
        state_holder["state"] = ServerState(
            agent=agent,
            broadcaster=Broadcaster(loop),
        )
        log.info("serve: ready (model=%s)", agent.provider.model if hasattr(agent.provider, "model") else "?")

    def state() -> ServerState:
        return state_holder["state"]

    # ----- index --------------------------------------------------------

    @app.get("/")
    async def index() -> FileResponse:
        if not index_html_path.exists():
            raise HTTPException(404, f"index.html not found at {index_html_path}")
        return FileResponse(str(index_html_path), media_type="text/html; charset=utf-8")

    # ----- SSE ---------------------------------------------------------

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        st = state()
        q = st.broadcaster.subscribe()

        async def gen():
            # 客户端连上后立刻同步一次状态，让 onopen 路径能拿到当前 running 等
            try:
                # 心跳 — 防止反向代理切断长连接
                last_beat = time.monotonic()
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        evt = await asyncio.wait_for(q.get(), timeout=15.0)
                        data = json.dumps(evt, ensure_ascii=False)
                        yield f"data: {data}\n\n"
                    except asyncio.TimeoutError:
                        # SSE 注释帧作为心跳
                        yield ": ping\n\n"
                    if time.monotonic() - last_beat > 30:
                        last_beat = time.monotonic()
            finally:
                st.broadcaster.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ----- history / context / status / todos -------------------------

    @app.get("/history")
    async def history() -> JSONResponse:
        return JSONResponse(_history_wire(state().agent.history))

    @app.get("/context")
    async def context_endpoint() -> JSONResponse:
        # 没有 token 计数器；返回字符长度作为 used 的近似，window 用一个保守默认。
        used = sum(len(m.content or "") + len(m.reasoning_content or "") for m in state().agent.history)
        return JSONResponse({"used": used // 4, "window": 128_000})

    @app.get("/status")
    async def status_endpoint() -> JSONResponse:
        st = state()
        prov: Provider = st.agent.provider
        label = getattr(prov, "model", "") or "unknown"
        used = sum(len(m.content or "") + len(m.reasoning_content or "") for m in st.agent.history) // 4
        return JSONResponse(
            {
                "label": label,
                "running": st.running,
                "plan": st.plan_mode,
                "toolApprovalMode": st.tool_approval_mode,
                "autoApproveTools": st.tool_approval_mode == "yolo",
                "bypass": st.tool_approval_mode == "yolo",
                "used": used,
                "window": 128_000,
                "cumulative": st.cumulative_usage,
                "lastUsage": st.last_usage,
                "goal": "",
                "goalStatus": "",
                "balance": None,
                "cacheHit": st.cumulative_usage.get("cacheHitTokens", 0),
                "cacheMiss": st.cumulative_usage.get("cacheMissTokens", 0),
            }
        )

    @app.get("/todos")
    async def todos_endpoint() -> JSONResponse:
        out = []
        for t in state().agent.todos():
            out.append(
                {
                    "content": t.get("content", ""),
                    "status": t.get("status", "pending"),
                    "activeForm": t.get("activeForm", ""),
                    "level": int(t.get("level", 0) or 0),
                }
            )
        return JSONResponse(out)

    # ----- stub GETs ---------------------------------------------------

    @app.get("/sessions")
    async def sessions_endpoint() -> JSONResponse:
        # 当前实现不持久化会话；返回空列表使前端 sidebar 渲染空。
        return JSONResponse([])

    @app.get("/skills")
    async def skills_endpoint() -> JSONResponse:
        return JSONResponse([])

    @app.get("/checkpoints")
    async def checkpoints_endpoint() -> JSONResponse:
        return JSONResponse([])

    @app.get("/branches")
    async def branches_endpoint() -> JSONResponse:
        return JSONResponse([])

    # ----- POSTs：核心 ----------------------------------------------------

    @app.post("/submit")
    async def submit(req: Request) -> Response:
        body = await _read_json(req)
        text = str(body.get("input", "")).strip()
        if not text:
            return Response(status_code=204)

        st = state()
        with st.lock:
            if st.running:
                return PlainTextResponse("a turn is already running", status_code=409)
            st.running = True
            st.cancel_flag.clear()
            st.worker = threading.Thread(
                target=_run_turn, args=(st, text), daemon=True, name="reasonix-py-turn"
            )
            st.worker.start()
        return Response(status_code=204)

    @app.post("/cancel")
    async def cancel() -> Response:
        state().cancel_flag.set()
        return Response(status_code=204)

    @app.post("/new")
    async def new_session() -> Response:
        st = state()
        with st.lock:
            if st.running:
                return PlainTextResponse("a turn is running; cancel first", status_code=409)
            st.agent.reset()
            st.cumulative_usage = {
                "promptTokens": 0,
                "completionTokens": 0,
                "totalTokens": 0,
                "cacheHitTokens": 0,
                "cacheMissTokens": 0,
            }
            st.last_usage = {}
        return Response(status_code=204)

    @app.post("/plan")
    async def plan_endpoint(req: Request) -> Response:
        body = await _read_json(req)
        state().plan_mode = bool(body.get("on"))
        return Response(status_code=204)

    @app.post("/tool-approval-mode")
    async def tool_approval_mode_endpoint(req: Request) -> Response:
        body = await _read_json(req)
        mode = str(body.get("mode", "ask"))
        if mode not in {"ask", "auto", "yolo"}:
            mode = "ask"
        state().tool_approval_mode = mode
        return Response(status_code=204)

    @app.post("/auto-approve-tools")
    async def auto_approve_tools_endpoint(req: Request) -> Response:
        body = await _read_json(req)
        state().tool_approval_mode = "yolo" if body.get("on") else "ask"
        return Response(status_code=204)

    @app.post("/bypass")
    async def bypass_endpoint(req: Request) -> Response:
        body = await _read_json(req)
        state().tool_approval_mode = "yolo" if body.get("on") else "ask"
        return Response(status_code=204)

    @app.post("/compact")
    async def compact_endpoint() -> Response:
        st = state()
        st.broadcaster.publish({"kind": "compaction_started", "compaction": {"trigger": "manual"}})
        st.broadcaster.publish({"kind": "compaction_done", "compaction": {"trigger": "manual"}})
        return Response(status_code=204)

    # ----- POSTs：stub（返回 204，避免前端报错） -----------------------

    async def _noop(req: Request) -> Response:
        try:
            await req.body()
        except Exception:
            pass
        return Response(status_code=204)

    for path in (
        "/answer",
        "/approve",
        "/goal",
        "/resume",
        "/forget",
        "/rewind",
        "/fork",
        "/summarize",
        "/delete-session",
    ):
        # 用闭包注册同样的 stub
        app.post(path)(_noop)

    return app


async def _read_json(req: Request) -> dict[str, Any]:
    try:
        body = await req.body()
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# CLI entrypoint: ``python -m reasonix_py.serve`` / ``reasonix-py-serve``
# ---------------------------------------------------------------------------


# index.html 的默认搜索路径（按优先级）：
#   --index 命令行参数  →  REASONIX_INDEX_HTML 环境变量  →
#   仓库内 internal/serve/index.html （仅在源码树里时存在）  →
#   reasonix_py 包目录里的 static/index.html （如果用户拷贝过去）

_DEFAULT_INDEX_CANDIDATES = [
    Path(__file__).resolve().parent.parent.parent / "internal" / "serve" / "index.html",
    Path(__file__).resolve().parent / "static" / "index.html",
]


def _resolve_index_html(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.getenv("REASONIX_INDEX_HTML", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    for cand in _DEFAULT_INDEX_CANDIDATES:
        if cand.exists():
            return cand
    return _DEFAULT_INDEX_CANDIDATES[0]  # 让 / 端点报 404，但 server 还能起


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reasonix-py-serve",
        description="Run the Reasonix-Py web UI (FastAPI + uvicorn).",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--index",
        default=None,
        help="Path to index.html. Defaults to the repo's internal/serve/index.html.",
    )
    parser.add_argument(
        "--reasoning-language",
        default="auto",
        choices=["auto", "zh", "en"],
    )
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-iterations", type=int, default=25)
    parser.add_argument(
        "--cors-origin",
        default=None,
        help="If set, allow this exact origin (dev only — server has no auth).",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    load_dotenv()

    try:
        provider = OpenAICompatProvider.from_env()
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 2

    agent = Agent(
        provider=provider,
        reasoning_language=args.reasoning_language,
        temperature=args.temperature,
        max_iterations=args.max_iterations,
    )

    index_html = _resolve_index_html(args.index)
    log.info("serve: index.html = %s (exists=%s)", index_html, index_html.exists())

    app = create_app(agent=agent, index_html_path=index_html, cors_origin=args.cors_origin)

    try:
        import uvicorn
    except ImportError:
        print("error: uvicorn is required; install with `pip install uvicorn[standard]`")
        return 2

    log.info("serve: http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
