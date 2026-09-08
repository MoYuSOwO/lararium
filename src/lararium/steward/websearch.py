"""联网搜索的出网层:发请求、认错、把服务商的报文收成我们自己的形状(M5-21)。

**这是 D2 的适配盒。** httpx 和 Tavily 的报文形状只出现在这里;`tools.py` 只看
`WebResult` 这一个 dataclass(F1:跨模块的数据要有名字,不传裸 dict)。换服务商动
这个文件,改渲染动 `tools.py`,两件事不再互相牵连。

**这一层不判信任、不渲染。** 搜回来的字当然是不可信内容,但围栏、折行、中和、
来源标注、拉高不可信闩全都在 `tools.web_search` 那**一个**出口——"两个出口两套规则"
这一节已经栽过两次(M4-4 的检索、M5-5 的读图),不再来第三次。

**失败一律收成 `WebSearchError`,而它的消息是给模型看的人话**(E2)。工具边界不许
抛异常给模型:模型看到「搜索超时,等一下再试」能自己决定下一步,看到 traceback 就
整轮炸掉,用户看到的是助手死了。所以异常映射必须发生在这里,不能漏给上面一个
`httpx.ConnectError`。

**key 只走 Authorization 头,不进 URL。** 进 URL 的东西会被代理、日志、服务商的
access log 原样留下,而那是用户自己掏钱的凭证;失败消息里也不带它(消息会被交给
模型、落进起居注)。
"""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

# Tavily 的检索端点。免费档 1000 次/月、不要信用卡,够单用户用,所以不做用量控制。
TAVILY_ENDPOINT = "https://api.tavily.com/search"

# 一次搜索最多等多久。实测这个端点 0.7 秒回一个 200,10 秒已经是"对面出事了"。
# 封顶是必须的:工具函数跑在框架给的线程池里,一次不设限的等待就是一个占着线程
# 不动的 worker,而队列是串行的——下一条消息在后面排队,用户看到的是助手不理人。
TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class WebResult:
    """一条搜索结果。**三个字段全是攻击者可控的文本**,`url` 也不例外——

    域名和路径都是对方自己定的,渲染时三样都要过 `neutralize_fence`(见 tools.py)。
    """

    title: str
    url: str
    text: str


class WebSearchError(Exception):
    """出网这一层的失败。**消息就是给模型看的那句人话**(E2)——`tools.py` 直接把它
    当返回值用,所以写的时候按"模型读了能决定下一步"来写,不是按日志来写。"""


class SearchPort(Protocol):
    """出网那一层的形状。**存在的理由只有一个:构造参数得有类型。**

    别把它当成插件体系的开端——没有注册表、没有配置项、没有按名字查找的地方。
    生产里的实现只有 `TavilySearch` 一个,测试里塞一个返回固定结果的假货,完。
    """

    def search(self, query: str, *, limit: int) -> list[WebResult]: ...


def parse_results(payload: Any) -> list[WebResult]:
    """把服务商的 JSON 收成 `WebResult` 列表。

    **报文是外部输入,形状一个都不许假设。** 缺字段、类型不对、整份不是 dict——
    一律收成空,不抛。一条畸形结果把整次搜索打成异常的话,真机上的面孔是"网络好着
    却查不了",而起居注里只有一个 KeyError,看不出是对面改了字段名。

    全空的条目跳掉:它渲染出来是一行「【(无标题)】 来源:(无来源链接)」,纯噪声。
    只缺一半的留着——有 url 就够用户自己点过去看。
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    results: list[WebResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        hit = WebResult(
            title=_text_field(item, "title"),
            url=_text_field(item, "url"),
            text=_text_field(item, "content"),
        )
        if hit.title or hit.url or hit.text:
            results.append(hit)
    return results


def _text_field(item: dict[str, Any], name: str) -> str:
    value = item.get(name)
    return value if isinstance(value, str) else ""


class TavilySearch:
    """Tavily 的适配。

    `transport` 是给测试的构造参数(httpx 自带的 `MockTransport` 就插在这儿),
    这样状态码映射、超时映射、报文解析整条链路都能在本地跑完,**一个真包都不发、
    一个真 key 都不需要**。生产里它是 None,httpx 用自己的默认传输。
    """

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = TAVILY_ENDPOINT,
        timeout: float = TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout = timeout
        self._transport = transport

    def search(self, query: str, *, limit: int) -> list[WebResult]:
        """同步发一次请求。**同步是对的**:工具函数本来就跑在框架给的线程池里,
        在这里开一个事件循环反而是给调度添一层。"""
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                response = client.post(
                    self._endpoint,
                    json={"query": query, "max_results": limit, "search_depth": "basic"},
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.TimeoutException as exc:
            raise WebSearchError(f"搜索超时({self._timeout:.0f} 秒没回应),等一下再试。") from exc
        except httpx.HTTPError as exc:
            # 只带异常**类名**,不带 str(exc):后者会把 URL 原样带出来,而失败消息
            # 会被交给模型、落进起居注。类名足够分辨是连不上还是读坏了。
            raise WebSearchError(f"搜索没连上({type(exc).__name__}),网络可能不通。") from exc

        _raise_for_status(response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            # 200 但正文是一页 HTML——网关插进来的错误页,真机上会遇到。
            raise WebSearchError("搜索服务回了看不懂的东西,这次查不了。") from exc
        return parse_results(payload)


def _raise_for_status(status: int) -> None:
    """状态码 → 人话。**要分得清是哪一类**,三种的下一步动作完全不一样:
    401/403 是"你的 key 该重配了",429 是"这个月额度用完了,等等",5xx 是"对面的事"。
    笼统一句「搜索失败」会让用户去查错的东西。
    """
    if status in (401, 403):
        raise WebSearchError("搜索服务不认这个 key,要重新配 LARARIUM_TAVILY_KEY。")
    if status == 429:
        raise WebSearchError("搜索服务限流了,这个月的免费额度可能用完了,等一会儿再试。")
    if status >= 400:
        raise WebSearchError(f"搜索服务回了 {status},这次查不了,等一下再试。")
