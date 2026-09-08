"""出网那一层(M5-21)。**一条测试都不需要真 key、不发一个真包。**

httpx 自带 `MockTransport`,构造参数塞进去就能把整条链路(拼报文 → 状态码 →
JSON → 收成 WebResult)在本地跑完。真实 API 的冒烟留给验收方,打法写在 REVIEW.md。
"""

import json

import httpx
import pytest

from lararium.steward.websearch import (
    TAVILY_EXTRACT_ENDPOINT,
    TavilyExtract,
    TavilySearch,
    WebResult,
    WebSearchError,
    parse_extract,
    parse_results,
)


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


# ── M5-22:`/extract` 是同一条出网层的第二个端点 ────────────────────────────
#
# 一样不需要真 key、不发一个真包。**这一节最要紧的一条是
# `test_the_only_outbound_destination_is_tavily`**:它把"SSRF 面归零不是防住了、
# 是根本没有"这句话钉成可执行的断言——模型给的 url 进的是 JSON body,不是请求地址。


def _extractor(handler) -> TavilyExtract:
    return TavilyExtract("tvly-fake", transport=httpx.MockTransport(handler))


def _extracted(text: str = "正文", url: str = "https://x.example/a") -> httpx.Response:
    return httpx.Response(200, json={"results": [{"url": url, "raw_content": text}]})


def test_extract_asks_for_markdown_and_keeps_the_key_out_of_the_url():
    """请求形状钉死:markdown、basic、url 进 body;key 只在 Authorization 头里。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.read())
        return _extracted()

    page = _extractor(handler).fetch("https://x.example/a", deep=False)

    assert seen["auth"] == "Bearer tvly-fake"
    assert "tvly-fake" not in seen["url"]
    assert seen["url"] == TAVILY_EXTRACT_ENDPOINT
    assert seen["body"]["urls"] == ["https://x.example/a"]
    assert seen["body"]["extract_depth"] == "basic"
    assert seen["body"]["format"] == "markdown"
    assert page.text == "正文"


def test_the_deep_flag_is_one_parameter_not_a_new_system():
    """兜底那一层 = `extract_depth` 换一个字,**不是一个新的取法**。

    出站目的地、鉴权、报文解析、异常映射全都一样——所以这里只该看到一个字段变了。
    """
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.read()))
        return _extracted()

    _extractor(handler).fetch("https://x.example/a", deep=False)
    _extractor(handler).fetch("https://x.example/a", deep=True)

    assert [b["extract_depth"] for b in seen] == ["basic", "advanced"]
    assert seen[0] | {"extract_depth": "advanced"} == seen[1], "除了深度还有别的东西变了"


def test_the_only_outbound_destination_is_tavily():
    """★ **模型给的 url 绝不进出站请求的地址**,它进的是 JSON body。

    这就是"SSRF 面归零不是'防住了'是'根本没有'"那句话的可执行版本:我们从头到尾
    不发起模型可控的出站请求,所以内网地址、DNS rebinding 全都没有作用对象。
    哪天有人"顺手改成自己抓",这条会红。
    """
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _extracted(text="")

    _extractor(handler).fetch("http://169.254.169.254/latest/meta-data/", deep=False)
    _extractor(handler).fetch("http://127.0.0.1:8420/v1/commands", deep=True)

    assert seen == [TAVILY_EXTRACT_ENDPOINT, TAVILY_EXTRACT_ENDPOINT]


def test_the_source_url_is_the_one_we_asked_for():
    """来源标注写**我们请求的那个** url,不是报文里回来的那个。

    后者是外部文本(服务商可以回任何东西),而这一行的用处是让用户点回去核对——
    得是他自己给的那条链接。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _extracted(url="https://evil.example/somewhere-else")

    assert _extractor(handler).fetch("https://x.example/a", deep=False).url == "https://x.example/a"


@pytest.mark.parametrize(
    ("status", "word"),
    [(401, "key"), (403, "key"), (429, "限流"), (500, "500"), (502, "502")],
)
def test_extract_failures_say_they_are_about_reading_a_page(status, word):
    """失败话要说清是**哪件事**失败了。读网页失败却说「搜索超时」,用户会去查错的东西
    ——那正是 M5-21 里"没配 key vs key 填错必须是两句话"的同一条理由。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    with pytest.raises(WebSearchError) as exc:
        _extractor(handler).fetch("https://x.example/a", deep=False)

    assert word in str(exc.value)
    assert "搜索" not in str(exc.value)


def test_extract_timeouts_and_connection_errors_become_readable_failures():
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(WebSearchError, match="超时"):
        _extractor(timeout).fetch("https://x.example/a", deep=False)
    with pytest.raises(WebSearchError, match="没连上"):
        _extractor(refused).fetch("https://x.example/a", deep=False)


def test_the_key_never_leaks_into_an_extract_failure_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error for key tvly-fake")

    with pytest.raises(WebSearchError) as exc:
        _extractor(handler).fetch("https://x.example/a", deep=False)

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
        {"results": [{"raw_content": None}]},
        {"failed_results": [{"url": "https://x.example/a", "error": "403"}]},
    ],
)
def test_a_malformed_extract_payload_yields_an_empty_page(payload):
    """畸形报文、抽取失败(`failed_results`)——都收成"一页空的",不抛。

    空是**上面那层的判据**:`web_fetch` 靠它决定升一次 advanced、再决定说哪一句话。
    在这里抛异常会把"我读不到"变成"网络出错了",两件事不一样。
    """
    page = parse_extract(payload, "https://x.example/a")

    assert page.text == ""
    assert page.url == "https://x.example/a", "抽不出来也要把来源留着"


def test_the_first_usable_result_wins():
    """一次只问一个 url,但报文里给几条都不奇怪(服务商的形状不许假设)。"""
    payload = {"results": [{"raw_content": ""}, {"title": "标题", "raw_content": "正文"}]}

    page = parse_extract(payload, "https://x.example/a")

    assert (page.title, page.text) == ("标题", "正文")


@pytest.mark.parametrize(
    ("status", "sentence"),
    [
        (401, "搜索服务不认这个 key,要重新配 LARARIUM_TAVILY_KEY。"),
        (429, "搜索服务限流了,这个月的免费额度可能用完了,等一会儿再试。"),
        (503, "搜索服务回了 503,这次查不了,等一下再试。"),
    ],
)
def test_the_search_wording_is_byte_for_byte_unchanged(status, sentence):
    """★ M5-22 把发请求 + 异常映射抽成了两个端点共用的一份。

    **抽完 `web_search` 必须逐字不变**,所以这三句按字面钉死当金样——共用那一层少
    参数化一个词、把「搜索」写成「抓取」,这条立刻红。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    with pytest.raises(WebSearchError) as exc:
        _client(handler).search("x", limit=3)

    assert str(exc.value) == sentence
