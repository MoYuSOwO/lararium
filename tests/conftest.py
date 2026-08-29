import os
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from lararium.config import Settings
from lararium.steward.model import PydanticAIClient

# 模块级抓取:下面那个 autouse fixture 会在每个测试前清掉所有 LARARIUM_*。
# conftest 在**收集期**导入,先把真实配置留在手里,live fixture 再原样喂回去。
_LIVE_ENV = {k: v for k, v in os.environ.items() if k.startswith("LARARIUM_")}


@pytest.fixture
def live_steward(live_steward_factory):
    """默认配置下的真模型 Steward。要改配置(如 M5-5 的 LARARIUM_VISION)用工厂那个。"""
    return live_steward_factory()


@pytest.fixture
def live_steward_factory(tmp_path, monkeypatch):
    """真 key、真模型,走**生产的组装根** `build_steward`,只把 data_dir 换到 tmp_path。

    没有 API key 就 skip——判定放在 fixture 里而不是模块级 mark,是因为本项目共享测试
    装置的唯一方式是 fixture 注入:`from conftest import ...` 依赖 tests/ 没有
    `__init__.py` 才成立,谁加一个就让整个收集失败(补2b Step 4 的教训)。

    `steward.ledger` / `steward.gate` 直接挂在实例上,验收账本与门控用它们即可。
    `make(**overrides)` 里传的环境变量覆盖 .env 里的那份。
    """

    def make(**overrides: str) -> Any:
        if not _LIVE_ENV.get("LARARIUM_API_KEY"):
            pytest.skip("真模型验收:需要 LARARIUM_API_KEY(先 set -a && source .env && set +a)")

        from bundles.memory.server import build_memory_components

        from lararium.config import Settings
        from lararium.gateway.server import build_steward

        for key, value in _LIVE_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("LARARIUM_DATA_DIR", str(tmp_path))
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)

        settings = Settings.load()
        ledger, gate = build_memory_components(settings.data_dir)
        return build_steward(settings, ledger, gate)

    return make


@pytest.fixture(autouse=True)
def _isolate_lararium_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉宿主环境里所有 LARARIUM_* 变量,让测试只看见自己设的值。"""
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
