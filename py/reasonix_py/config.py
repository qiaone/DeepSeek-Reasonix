"""Lightweight config / env loader.

只做最小够用的事：读 ``.env``（如果存在）→ 不覆盖已有的 ``os.environ``。
更高级的 reasonix.toml / 多档 profile，留给后续扩展。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def load_dotenv(path: Optional[str | Path] = None) -> bool:
    """Best-effort ``.env`` loader (no extra dependency).

    - 不覆盖已经存在的环境变量
    - 支持 ``KEY=VALUE`` 与 ``KEY="value with spaces"``
    - 不解析 shell 替换、不导入 python-dotenv
    """
    p = Path(path) if path else Path.cwd() / ".env"
    if not p.exists():
        return False
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val
    return True
