     # Reasonix-Py · Python 最小内核

本目录是 Reasonix（Go 版）核心思想的 **Python 复刻**。目标是用最少的代码量呈现一个可独立运行、可扩展的"代码代理（coding agent）"骨架，便于后续在此基础上继续完善（plan 模式、子代理、记忆、压缩……）。

**这不是 Go 代码的逐行翻译。** 仅复刻了让一个 agent 能"对话 + 调工具 + 跑循环"必需的最小集合，其它能力（plan、goal、coordinator、checkpoint、压缩、MCP、output-style 等）在文档末尾标记为可扩展点。

## 目录结构

```
py/
├── README.md                        ← 本文件
├── pyproject.toml                   ← 包元信息与依赖
├── requirements.txt                 ← 运行时依赖（httpx）
├── reasonix_py/
│   ├── __init__.py
│   ├── __main__.py                  ← `python -m reasonix_py` 入口（REPL）
│   ├── prompts.py                   ← DefaultSystemPrompt / UserDecisionPolicy / LanguagePolicy（与 Go 字节一致）
│   ├── messages.py                  ← Role / Message / ToolCall / 历史规整（NormalizeMessages 简化版）
│   ├── provider.py                  ← Provider 抽象 + Chunk 流式事件 + OpenAI 兼容实现
│   ├── tools.py                     ← Tool ABC + Registry + JSON-Schema 装饰器
│   ├── builtins.py                  ← 内置工具：read_file / write_file / ls / bash / todo_write
│   ├── agent.py                     ← Agent 主循环：流式 → 收集 tool_calls → 执行 → 再发
│   ├── serve.py                     ← FastAPI + uvicorn web 后端（复用 internal/serve/index.html）
│   └── config.py                    ← 配置加载（环境变量 + 简单 dict）
└── examples/
    ├── hello.py                     ← 最小调用示例
    └── smoke_serve.py               ← 不联网的 ASGI 烟雾测试
```

## 与 Go 内核的对应关系

| Go 包 | Python 模块 | 复刻范围 |
|---|---|---|
| `internal/config/config.go`（DefaultSystemPrompt, UserDecisionPolicy, LanguagePolicy） | `prompts.py` | **完整**，常量字节一致 |
| `internal/provider/provider.go`（Provider, Message, Chunk, ToolCall, NormalizeMessages） | `provider.py` + `messages.py` | **核心**：流式接口、OpenAI 兼容、tool-call 配对修复（简化版） |
| `internal/provider/openai/*` | `provider.py::OpenAICompatProvider` | OpenAI Chat Completions / DeepSeek / 任何 `/chat/completions` 兼容端点 |
| `internal/tool/tool.go`（Tool, Registry） | `tools.py` | **完整**抽象；canonicalize 简化为直接序列化 |
| `internal/tool/builtin/*` | `builtins.py` | 仅复刻 5 个：`read_file` / `write_file` / `ls` / `bash` / `todo_write` |
| `internal/agent/agent.go`（主循环） | `agent.py` | **核心循环**：发请求 → 流 → 工具调用并执行 → 追加消息 → 再循环。未实现 plan/goal/compact/coordinator/cancel/branch |
| `internal/serve/serve.go` + `index.html` | `serve.py` + 复用同一份 `index.html` | 复刻前端真正调用的端点子集（GET：`/`、`/events`、`/history`、`/context`、`/status`、`/todos`、`/sessions`、`/skills`、`/checkpoints`、`/branches`；POST：`/submit`、`/cancel`、`/new`、`/plan`、`/tool-approval-mode`、`/auto-approve-tools`、`/bypass`、`/compact`、`/answer`、`/approve`、`/goal`、`/resume`、`/forget`、`/rewind`、`/fork`、`/summarize`、`/delete-session`）。未落地的功能以 stub 形式返回兼容值，保证前端可正常渲染 |
| `internal/boot/boot.go`（system prompt 拼接） | `agent.py::build_system_prompt` | 顺序：base → UserDecisionPolicy → LanguagePolicy（不含 outputstyle / memory / skills） |

## 设计要点（与 Go 版一致的"硬约束"）

1. **Cache-first 前缀稳定**：`build_system_prompt` 在一个 session 内只算一次，禁止中途修改。后续要加 outputstyle / memory，应该走 user-turn 末尾追加，而不是改 system 槽（详见 Go 的 `control.Compose`）。
2. **UserDecisionPolicy / LanguagePolicy 永远追加**：即使用户传了自定义 system prompt，这两条仍以 `\n\n` 拼到末尾（`build_system_prompt` 的不变量）。
3. **工具调用契约**：每个 `assistant` 带 `tool_calls` 的消息必须紧跟相同数量的 `tool` 消息（按 `tool_call_id` 配对）。`messages.normalize_messages` 在发请求前自动修补。
4. **read-only 并行 / 写 串行**：`Tool.read_only` 决定一批 tool_calls 是否能并行执行（见 `agent.py::_run_tools`）。
5. **Stream-first**：Provider 必须流式输出 `Chunk`，agent 把所有 chunk 合并成一条 `assistant` 消息再决定下一步。

## 安装

环境：`C:\Users\Admin\miniforge3\envs\py312`

```powershell
& "C:\Users\Admin\miniforge3\envs\py312\python.exe" -m pip install -r py\requirements.txt
```

或开发安装：

```powershell
& "C:\Users\Admin\miniforge3\envs\py312\python.exe" -m pip install -e py
```

## 快速开始

设置环境变量：

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."        # 或 OPENAI_API_KEY
$env:REASONIX_BASE_URL = "https://api.deepseek.com/v1"
$env:REASONIX_MODEL    = "deepseek-chat"
```

启动 REPL：

```powershell
& "C:\Users\Admin\miniforge3\envs\py312\python.exe" -m reasonix_py
```

启动 Web UI（FastAPI + uvicorn，复用 Go 端的 `internal/serve/index.html`）：

```powershell
& "C:\Users\Admin\miniforge3\envs\py312\python.exe" -m pip install "fastapi>=0.110" "uvicorn[standard]>=0.27"
& "C:\Users\Admin\miniforge3\envs\py312\python.exe" -m reasonix_py.serve --host 127.0.0.1 --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

需要自定义前端时，传 `--index path\to\index.html` 或设环境变量 `REASONIX_INDEX_HTML`。

或在脚本中嵌入：

```python
from reasonix_py.agent import Agent
from reasonix_py.provider import OpenAICompatProvider
from reasonix_py.tools import Registry
from reasonix_py.builtins import default_registry

agent = Agent(
    provider=OpenAICompatProvider.from_env(),
    tools=default_registry(),
)
for event in agent.run("把当前目录下的 README.md 总结成 3 句话"):
    print(event, end="", flush=True)
```

## 已实现 / 待扩展

✅ 已实现（最小核心）
- DefaultSystemPrompt + 两条 Policy 强制追加
- OpenAI 兼容 streaming（含 `tool_calls` 增量合并）
- Tool 抽象 + Registry + JSON Schema
- 5 个内置工具（read_file / write_file / ls / bash / todo_write）
- Agent 主循环（多轮、并行 read-only 工具、串行 write 工具）
- 基础消息历史规整（修复未配对的 tool_calls）

🟡 易于添加（已留扩展点）
- 更多内置工具：grep / glob / edit_file / multi_replace / web_fetch
- ReasoningLanguageBlock：`agent.py::_inject_user_turn` 改一行即可注入
- Output style / persona 切换：`build_system_prompt` 加分支
- 项目记忆（REASONIX.md / AGENTS.md）：在 `build_system_prompt` 末尾 compose
- MCP 工具：实现 `Tool` 子类，`mcp__<server>__<tool>` 命名注册到 Registry

🔴 较大改动（暂未规划）
- Plan 模式（写器屏蔽）
- Goal 模式（伪用户回合 FSM）
- Coordinator / Subagent
- 上下文压缩（Compaction）
- Checkpoint / 时光机
- Token-economy 模式
- HTTP/SSE serve、Wails 桌面端

## 参考

- Reasonix Go 内核：[`../internal/`](../internal/)
- 全量 prompts 文档：[`../docs/prompts/`](../docs/prompts/)
- 项目 memory：[`../REASONIX.md`](../REASONIX.md)
