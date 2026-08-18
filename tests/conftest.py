from collections.abc import Callable
from typing import Any

import httpx
import pytest

from lararium.config import Settings
from lararium.steward.model import PydanticAIClient


@pytest.fixture(autouse=True)
def _isolate_lararium_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉宿主环境里所有 LARARIUM_* 变量,让测试只看见自己设的值。"""
    import os

    for key in list(os.environ):
        if key.startswith("LARARIUM_"):
            monkeypatch.delenv(key, raising=False)


def text_reply(content: str = "ok") -> dict[str, Any]:
    """共用的文本回复体,wire 测试与验收测试共享,避免复制两份 JSON(CONVENTIONS S)。"""
    return {
        "id": "1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def tool_call_reply() -> dict[str, Any]:
    """共用的工具调用回复体:模型先要求调 current_time,让框架再发一轮。"""
    return {
        "id": "1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "current_time", "arguments": "{}"},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


@pytest.fixture
def reply_factories():
    """返回 text_reply / tool_call_reply 两个构造器。

    用 fixture 注入,避免 `from conftest import ...`——那依赖 tests/ 没有
    __init__.py 才成立,任何人加个 __init__.py 就让整个收集失败(补2b Step 4)。
    """
    return text_reply, tool_call_reply


@pytest.fixture
def http_spy_factory(monkeypatch):
    """返回一个能构造"只换 HTTP 传输"的 PydanticAIClient 的工厂。

    报文级测试都要用真实 OpenAIChatModel + MockTransport 抓 body;把这层接线
    收进 conftest,用 fixture 注入而非 `from conftest import ...`——后者的路径
    依赖 tests/ 没有 __init__.py 才成立,是脆的。

    走 `PydanticAIClient(settings, http_client=...)` 的**真实生产构造路径**:
    AsyncOpenAI 带 max_retries=0(M2-4 Step0a),和线上同一段代码,不是测试专用的
    平行构造——测的才真。
    """
    monkeypatch.setenv("LARARIUM_API_KEY", "sk-test")

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> PydanticAIClient:
        settings = Settings.load()
        return PydanticAIClient(
            settings,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    return factory
