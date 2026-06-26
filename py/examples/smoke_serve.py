"""ASGI smoke test — no network. Runs against an in-memory fake provider."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # ./py on path

from fastapi.testclient import TestClient

from reasonix_py.agent import Agent
from reasonix_py.provider import Chunk, ChunkType, Provider
from reasonix_py.serve import create_app


class Dummy(Provider):
    name = "dummy"
    model = "dummy-1"

    def stream(self, req):
        yield Chunk(type=ChunkType.TEXT, text="hello world")


def main() -> int:
    agent = Agent(provider=Dummy())
    index = Path(__file__).resolve().parent.parent.parent / "internal" / "serve" / "index.html"
    app = create_app(agent=agent, index_html_path=index)

    with TestClient(app) as c:
        r = c.get("/")
        print("GET /", r.status_code, r.headers.get("content-type"))
        print("GET /history", c.get("/history").json())
        s = c.get("/status").json()
        print("GET /status", s["label"], "running=", s["running"])
        print("GET /todos", c.get("/todos").json())
        print("GET /sessions", c.get("/sessions").json())
        r = c.post("/plan", json={"on": True})
        print("POST /plan", r.status_code, r.text[:300])
        print("plan now =", c.get("/status").json()["plan"])
        r = c.post("/tool-approval-mode", json={"mode": "yolo"})
        print("POST /tool-approval-mode", r.status_code, r.text[:300])
        print("mode now =", c.get("/status").json()["toolApprovalMode"])
        print("POST /new", c.post("/new").status_code)
        print("POST /answer stub", c.post("/answer", json={}).status_code)
        print("POST /submit", c.post("/submit", json={"input": "hi"}).status_code)
        # 等 turn 跑完
        import time
        for _ in range(50):
            if not c.get("/status").json()["running"]:
                break
            time.sleep(0.05)
        print("history after submit =", c.get("/history").json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
