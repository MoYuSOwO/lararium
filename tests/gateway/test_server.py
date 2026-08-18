import asyncio
import inspect
from pathlib import Path

import pytest
from bundles.memory.server import build_memory_components, memory_tool_functions
from starlette.testclient import TestClient

from lararium.config import Settings
from lararium.db import connect
from lararium.gateway.server import create_app
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal
from lararium.steward.loop import Steward
from lararium.steward.model import ModelReply
from lararium.steward.outbox import Outbox
from lararium.steward.registry import Registry


class FakeModel:
    """记录收到的上下文,返回固定回复。不联网。"""

    def __init__(self, text="你好呀"):
        self._text = text
        self.seen = []

    async def run(self, ctx, tools, mcp_servers):
        self.seen.append(ctx)
        return ModelReply(text=self._text)


TOKENS = {"cli": "tok-cli", "web": "tok-web"}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")
    monkeypatch.setenv("LARARIUM_DATA_DIR", str(tmp_path))
    settings = Settings.load()
    conn = connect(tmp_path / "steward.sqlite")
    ledger, gate = build_memory_components(tmp_path)
    steward = Steward(
        settings=settings,
        inbox=Inbox(conn),
        journal=Journal(conn),
        registry=Registry.load(Path("bundles")),
        ledger=ledger,
        gate=gate,
        model=FakeModel(),
        persona="你是 Lararium。",
        outbox=Outbox(conn),
        bundle_tools=memory_tool_functions(gate),
    )
    wake = asyncio.Event()
    app = create_app(steward=steward, ledger=ledger, gate=gate, tokens=TOKENS, wake=wake)
    # 不用 TestClient 上下文(不进 lifespan),worker 不启动——API 契约测试保持确定性。
    return app, steward


def test_no_token_or_wrong_token_returns_generic_401(server):
    app, _ = server
    client = TestClient(app)
    no = client.post("/v1/messages", json={"content": "hi"})
    wrong = client.post(
        "/v1/messages",
        json={"content": "hi"},
        headers={"Authorization": "Bearer nope"},
    )
    assert no.status_code == 401
    assert wrong.status_code == 401
    # 同一句话,不区分"没有 token / token 错",不回显内部细节
    assert no.text == wrong.text
    assert "api_key" not in no.text.lower()
    assert "traceback" not in no.text.lower()


def test_post_message_maps_token_to_channel_and_ignores_forged_channel(server):
    app, steward = server
    client = TestClient(app)
    r = client.post(
        "/v1/messages",
        json={"content": "你好", "channel": "web"},  # token=tok-cli → 渠道必须是 cli,伪造无效
        headers={"Authorization": "Bearer tok-cli"},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["duplicate"] is False
    env_id = body["envelope_id"]
    row = steward.inbox._conn.execute(
        "SELECT channel, state FROM inbox WHERE id=?", (env_id,)
    ).fetchone()
    assert row["channel"] == "cli"


def test_duplicate_post_same_id_only_processed_once(server):
    app, steward = server
    client = TestClient(app)
    env_id = "a" * 32
    h = {"Authorization": "Bearer tok-cli"}
    r1 = client.post("/v1/messages", json={"id": env_id, "content": "第一条"}, headers=h)
    r2 = client.post("/v1/messages", json={"id": env_id, "content": "第一条"}, headers=h)
    assert r1.status_code == 202 and r1.json()["duplicate"] is False
    assert r2.status_code == 202 and r2.json()["duplicate"] is True
    n = steward.inbox._conn.execute("SELECT COUNT(*) FROM inbox WHERE id=?", (env_id,)).fetchone()[
        0
    ]
    assert n == 1


def test_post_oversized_content_returns_413(server):
    app, _ = server
    client = TestClient(app)
    big = "x" * (17 * 1024)  # 17KB > 16KB 上限
    r = client.post(
        "/v1/messages", json={"content": big}, headers={"Authorization": "Bearer tok-cli"}
    )
    assert r.status_code == 413


def test_post_non_json_or_missing_content_returns_400(server):
    app, _ = server
    client = TestClient(app)
    h = {"Authorization": "Bearer tok-cli"}
    not_json = client.post("/v1/messages", content="not json", headers=h)
    assert not_json.status_code == 400
    empty = client.post("/v1/messages", json={}, headers=h)
    assert empty.status_code == 400


def test_outbox_scopes_to_channel_and_respects_after(server):
    app, steward = server
    # 直接播种出件箱(不靠 worker),确定性测 outbox 端点的读取/过滤逻辑
    steward.outbox.put("env-1", "cli", "cli 回复1")
    steward.outbox.put("env-2", "web", "web 回复")
    steward.outbox.put("env-3", "cli", "cli 回复2")
    client = TestClient(app)
    h = {"Authorization": "Bearer tok-cli"}

    r = client.get("/v1/outbox", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["content"] for i in items] == ["cli 回复1", "cli 回复2"], "应只返回本渠道"

    first_seq = items[0]["seq"]
    r2 = client.get(f"/v1/outbox?after={first_seq}", headers=h)
    assert [i["content"] for i in r2.json()["items"]] == ["cli 回复2"], "after 过滤应生效"


def test_health_returns_counts(server):
    app, _ = server
    client = TestClient(app)
    r = client.get("/v1/health", headers={"Authorization": "Bearer tok-cli"})
    assert r.status_code == 200
    body = r.json()
    assert "pending" in body and "unsettled" in body


def test_every_http_handler_is_an_async_function(server):
    """全局约束第 1 条:HTTP 处理函数一律 async def——check_same_thread=False 的前提。

    同步 handler 会被 starlette 丢进线程池,连接就真的跨线程并发了。机械地守。
    """
    app, _ = server
    for route in app.routes:
        ep = getattr(route, "endpoint", None)
        if ep is not None and callable(ep):
            assert inspect.iscoroutinefunction(ep), (
                f"{getattr(route, 'path', '?')} 的 handler 不是协程函数(async def)"
            )
