import os
from collections.abc import Callable
from typing import Any

import httpx
import pytest


@pytest.fixture(autouse=True)
def _isolate_lararium_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉宿主环境里所有 LARARIUM_* 变量,让测试只看见自己设的值。"""
    for key in list(os.environ):
        if key.startswith("LARARIUM_"):
            monkeypatch.delenv(key, raising=False)


def build_http_spy_client(
    handler: Callable[[httpx.Request], httpx.Response], *, api_key: str = "sk-test"
) -> Any:
    """报文级测试的帮助:真实 PydanticAIClient + 真实 OpenAIChatModel,
    只把 HTTP 传输换成 MockTransport。返回底层能捕获 body 的 client。

    用例(wire 测试、验收①报文级复核)共享这一份,避免复制两份夹具(CONVENTIONS S)。
    """
    from lararium.config import Settings

    settings = Settings.load()
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    model = OpenAIChatModel(
        settings.model_name,
        provider=OpenAIProvider(
            base_url=settings.api_base_url,
            api_key=api_key,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
    )
    from lararium.steward.model import PydanticAIClient

    return PydanticAIClient(settings, model=model)
