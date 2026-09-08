"""出网那一层(M5-21)。**一条测试都不需要真 key、不发一个真包。**

httpx 自带 `MockTransport`,构造参数塞进去就能把整条链路(拼报文 → 状态码 →
JSON → 收成 WebResult)在本地跑完。真实 API 的冒烟留给验收方,打法写在 REVIEW.md。
"""

import httpx
import pytest

from lararium.steward.websearch import TavilySearch, WebResult, WebSearchError, parse_results


def _client(handler) -> TavilySearch:
    return TavilySearch("tvly-fake", transport=httpx.MockTransport(handler))


def test_the_key_goes_in_the_header_and_never_in_the_query_string():
    """key 只许出现在 Authorization 头里。

    进 URL 的东西会被代理、被日志、被服务商的 access log 原样留下——而这是用户的
    付费凭证。顺带把请求形状钉住:query 和 max_results 是我们说了算的。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"results": []})

    _client(handler).search("上海天气", limit=3)

    assert seen["auth"] == "Bearer tvly-fake"
    assert "tvly-fake" not in seen["url"]
    assert '"max_results": 3' in seen["body"] or '"max_results":3' in seen["body"]


def test_search_returns_named_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "上海天气", "url": "https://w.example/sh", "content": "周六晴"}
                ]
            },
        )

    assert _client(handler).search("上海天气", limit=3) == [
        WebResult(title="上海天气", url="https://w.example/sh", text="周六晴")
    ]


@pytest.mark.parametrize(
    ("status", "word"),
    [(401, "key"), (403, "key"), (429, "限流"), (500, "500"), (502, "502")],
)
def test_bad_status_codes_become_readable_failures(status, word):
    """服务商回什么码都得能说成人话,而且要**说清是哪一类**——401 是 key 的事、
    429 是额度的事、5xx 是对面的事,三种的下一步动作不一样。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    with pytest.raises(WebSearchError) as exc:
        _client(handler).search("上海天气", limit=3)

    assert word in str(exc.value)


def test_timeouts_and_connection_errors_become_readable_failures():
    """网络不通不是异常路径,是这条工具的日常。"""

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(WebSearchError, match="超时"):
        _client(timeout).search("x", limit=3)
    with pytest.raises(WebSearchError, match="没连上"):
        _client(refused).search("x", limit=3)


def test_a_non_json_body_becomes_a_readable_failure():
    """200 但正文是一页 HTML(网关插进来的错误页)——照样不许把异常抛给模型。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    with pytest.raises(WebSearchError, match="看不懂"):
        _client(handler).search("x", limit=3)


def test_the_key_never_leaks_into_the_failure_message():
    """失败消息会被原样交给模型、落进起居注、可能被用户看到。key 不许出现在里面。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error for key tvly-fake")

    with pytest.raises(WebSearchError) as exc:
        _client(handler).search("x", limit=3)

    assert "tvly-fake" not in str(exc.value)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "results",
        {},
        {"results": None},
        {"results": "not a list"},
        {"results": [None, 42, "x"]},
        {"results": [{}]},
    ],
)
def test_a_malformed_payload_yields_nothing_instead_of_exploding(payload):
    """服务商的 JSON 是**外部输入**,形状不许假设。

    一条畸形结果不该把整次搜索打成异常——那会变成"网络好着却查不了",而且起居注里
    只有一个 KeyError,看不出是对面改了字段名。
    """
    assert parse_results(payload) == []


def test_missing_fields_do_not_drop_the_whole_result():
    """字段缺一半的结果还是有用的(有 url 就能让用户自己去看),别整条丢掉。"""
    hits = parse_results({"results": [{"url": "https://x.example/a"}, {"content": "正文"}]})

    assert [h.url for h in hits] == ["https://x.example/a", ""]
    assert [h.text for h in hits] == ["", "正文"]
