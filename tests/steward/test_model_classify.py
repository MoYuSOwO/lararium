"""分类器补测试(Step 0):_classify_retryable 零覆盖的补丁。

M2-2 的四条 loop 测试都手工构造 ModelCallError(retryable=...),只证明
"loop 对旗子反应正确",没证明旗子在真实 HTTP 链路上被正确竖起来。这里用
http_spy_factory(真实 OpenAIChatModel + MockTransport)驱动 PydanticAIClient.run,
让 pydantic-ai 在真实传输上抛异常,再断言隔离盒分类出的 retryable 旗子。
"""

import httpx
import pytest
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from lararium.steward.assembler import AssembledContext
from lararium.steward.model import ModelCallError


def _ctx() -> AssembledContext:
    # 报文级测试的最小上下文:run 只消费 messages。
    return AssembledContext(system_prompt="", messages=[{"role": "user", "content": "hi"}])


async def test_http_429_is_retryable(http_spy_factory):
    """429 限流是临时失败,重试有意义。"""

    def handler(req):
        return httpx.Response(429, json={"error": "rate limited"}, request=req)

    client = http_spy_factory(handler)
    with pytest.raises(ModelCallError) as ei:
        await client.run(_ctx(), [], [])
    assert ei.value.retryable is True
    assert isinstance(ei.value.__cause__, ModelHTTPError)


async def test_http_5xx_is_retryable(http_spy_factory):
    """5xx 服务端错误是临时失败。"""

    def handler(req):
        return httpx.Response(503, json={"error": "unavailable"}, request=req)

    client = http_spy_factory(handler)
    with pytest.raises(ModelCallError) as ei:
        await client.run(_ctx(), [], [])
    assert ei.value.retryable is True


async def test_http_401_is_terminal(http_spy_factory):
    """401 key 错重试一万次也没用——终态。"""

    def handler(req):
        return httpx.Response(401, json={"error": "unauthorized"}, request=req)

    client = http_spy_factory(handler)
    with pytest.raises(ModelCallError) as ei:
        await client.run(_ctx(), [], [])
    assert ei.value.retryable is False


async def test_http_422_is_terminal(http_spy_factory):
    """422 请求非法(如上下文超长)——终态。"""

    def handler(req):
        return httpx.Response(422, json={"error": "invalid request"}, request=req)

    client = http_spy_factory(handler)
    with pytest.raises(ModelCallError) as ei:
        await client.run(_ctx(), [], [])
    assert ei.value.retryable is False


async def test_connection_error_is_retryable_via_unknown_default(http_spy_factory):
    """网络错到达隔离盒时是 ModelAPIError 而非 ModelHTTPError(P2-3 实测)。

    它落在「认不出的默认可重试」那条不对称规则上,不是纸面保险。
    """

    def handler(req):
        raise httpx.ConnectError("connection refused")

    client = http_spy_factory(handler)
    with pytest.raises(ModelCallError) as ei:
        await client.run(_ctx(), [], [])
    assert ei.value.retryable is True
    cause = ei.value.__cause__
    assert isinstance(cause, ModelAPIError)
    assert not isinstance(cause, ModelHTTPError), "连接错不该带 status code"
