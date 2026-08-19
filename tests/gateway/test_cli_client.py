"""CLI 客户端测试(M2-6)。

用真实 uvicorn(后台线程)起 FakeModel 服务,走真 socket + lifespan + worker——
Client 打的不是 mock,是和自己线上同一个栈的完整往返。
"""

import asyncio
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from bundles.memory.server import build_memory_components, memory_tool_functions

from lararium.config import Settings
from lararium.db import connect
from lararium.gateway.cli import Client
from lararium.gateway.server import create_app
from lararium.steward.inbox import Inbox
from lararium.steward.journal import Journal
from lararium.steward.loop import Steward
from lararium.steward.model import ModelReply
from lararium.steward.outbox import Outbox
from lararium.steward.registry import Registry
from lararium.steward.threads import Threads


class FakeModel:
    def __init__(self, text="你好呀"):
        self._text = text

    async def run(self, ctx, tools, mcp_servers):
        return ModelReply(text=self._text)


class _UvicornServer:
    """后台线程跑真实 uvicorn(port 0 临时端口),返回可连 URL。"""

    def __init__(self, app):
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> str:
        self._thread.start()
        while not self._server.started:
            time.sleep(0.005)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture
def live_url(tmp_path, monkeypatch):
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
        threads=Threads(conn),
        bundle_tools=memory_tool_functions(gate),
    )
    app = create_app(
        steward=steward,
        ledger=ledger,
        gate=gate,
        control_tokens={"cli": "tok-cli"},
        ingest_tokens={},
        wake=asyncio.Event(),
    )
    srv = _UvicornServer(app)
    url = srv.start()
    yield url
    srv.stop()


def test_chat_roundtrip_returns_reply(live_url):
    client = Client(live_url, "tok-cli")
    try:
        env_id = client.send_message("你好")
        reply = client.poll_reply(env_id, timeout_s=5)
        assert reply == "你好呀"
    finally:
        client.close()


def test_command_returns_text(live_url):
    client = Client(live_url, "tok-cli")
    try:
        assert client.command("/pending") == "无待审"
    finally:
        client.close()


def test_after_cursor_prevents_reconsuming(live_url):
    """一次往返后 after 前进到该批末尾;再 poll 不该重拉旧货(客户端按 seq 去重)。"""
    client = Client(live_url, "tok-cli")
    try:
        env1 = client.send_message("第一问")
        assert client.poll_reply(env1, timeout_s=5) == "你好呀"
        assert client.after > 0
        # 没有新货时,带着已推进的 after 再 poll,应返回空、after 不变
        before = client.after
        empty = client._poll_outbox(wait_s=1)
        assert empty == []
        assert client.after == before
    finally:
        client.close()


def test_wrong_token_raises_permission_error(live_url):
    client = Client(live_url, "bad-token")
    try:
        with pytest.raises(PermissionError):
            client.send_message("你好")
    finally:
        client.close()


def test_quit_command_returns_hint_and_client_survives(live_url):
    """HTTP 语境 /quit 只返回提示,服务不退、客户端继续可用(零副作用由服务端保证)。"""
    client = Client(live_url, "tok-cli")
    try:
        assert "服务端无退出概念" in client.command("/quit")
        assert client.command("/pending") == "无待审"  # 仍活着
    finally:
        client.close()


def test_r2_3_poll_reply_prints_bypassed_reply(monkeypatch, capsys):
    """R2-3:非目标信封的 reply 打印出来(旁路),别静默丢——出件箱按 seq 单调消费,
    游标推过去就取不回来了(M4 晨报/IM 会是常态)。"""
    from lararium.gateway.cli import Client

    c = Client("http://127.0.0.1:9", "tok")  # 不会真连(下面 monkeypatch 掉 _poll_outbox)
    served = iter(
        [
            [{"seq": 1, "envelope_id": "other", "kind": "reply", "content": "晨报:今天有雨"}],
            [{"seq": 2, "envelope_id": "mine", "kind": "reply", "content": "哈喽"}],
        ]
    )
    monkeypatch.setattr(c, "_poll_outbox", lambda wait_s: next(served))
    got = c.poll_reply("mine", timeout_s=1)
    assert got == "哈喽"
    out = capsys.readouterr().out
    assert "晨报" in out, "旁路的 reply 必须打印,不能静默丢"
