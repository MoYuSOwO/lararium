"""联网的出网层:发请求、认错、把服务商的报文收成我们自己的形状(M5-21 搜索,M5-22 读页)。

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

**M5-22:读网页走服务商的 `/extract`,我们不自己抓。** 于是这一层有两个端点、
一份请求与异常映射(`_post_json`),而**出站目的地只有 `api.tavily.com` 一个,写死在
本文件里**——模型给的 url 进的是 JSON body,永远不是请求地址。SSRF 面不是"防住了",
是**根本没有作用对象**:没有模型可控的出站请求,禁区网段、DNS rebinding、
`getpeername` 复查这些东西一个都不需要,写了反而会让下一个人以为这里有出站。
(推翻"自己抓"的那次实测记在 PLAN M5-22:挡我们的是反爬、登录墙、JS 渲染,
不是地理位置;能抓的 `/extract` 一样能抓,抓不到的自己抓也照样抓不到。)
"""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

# Tavily 的检索端点。免费档 1000 次/月、不要信用卡,够单用户用,所以不做用量控制。
TAVILY_ENDPOINT = "https://api.tavily.com/search"

# 读网页(M5-22)。`/extract` 是 1 credit / 5 个 URL、advanced 2 credit,免费档一个月
# 5000 次(advanced 2500),同样够单用户用。**这两条常量是全系统仅有的两个出站目的地。**
TAVILY_EXTRACT_ENDPOINT = "https://api.tavily.com/extract"

# 一次搜索最多等多久。实测这个端点 0.7 秒回一个 200,10 秒已经是"对面出事了"。
# 封顶是必须的:工具函数跑在框架给的线程池里,一次不设限的等待就是一个占着线程
# 不动的 worker,而队列是串行的——下一条消息在后面排队,用户看到的是助手不理人。
TIMEOUT_SECONDS = 10.0

# 抽取比搜索慢:对面要真去把页面取回来(advanced 还要渲染)。所以放宽到 20 秒,
# 但**理由和上面那条一模一样,不许不设限**。注意最坏情况是两倍:抽不出正文时
# 自动升一次 advanced(只升一次),那一轮最多占住线程 40 秒——这是"自动升一次"
# 换来的代价,认它;真要嫌久,该调的是这个数,不是把升级做成循环。
EXTRACT_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class WebResult:
    """一条从网上拿回来的东西:搜索结果的一条,或者(M5-22)读回来的一整页。

    **三个字段全是攻击者可控的文本**,`url` 也不例外——域名和路径都是对方自己定的,
    渲染时三样都要过 `neutralize_fence`(见 tools.py)。

    搜和读**共用这一个形状**是有意的:两边的渲染必须是同一段代码,而同一段代码只能
    吃同一种数据。「两个出口两套规则」这一节栽过三次(M4-4、M5-5、M5-21 的标题漏折行),
    形状先合并,渲染才合得起来。
    """

    title: str
    url: str
    text: str


class WebSearchError(Exception):
    """出网这一层的失败(搜索和读页共用)。**消息就是给模型看的那句人话**(E2)——
    `tools.py` 直接把它当返回值用,所以写的时候按"模型读了能决定下一步"来写,
    不是按日志来写。"""


@dataclass(frozen=True)
class _Wording:
    """失败话里随场合变的那两个词:「搜索超时」还是「抓取超时」。

    共用一份异常映射的代价是措辞会跟着串味——而 M5-21 立的规矩正是"笼统一句会让
    用户去查错的东西"。读网页失败却说「搜索服务限流了」,人会跑去查搜索。
    所以共用的是**逻辑**,不是字面:两个词从这里注进去,搜索那一组的取值必须
    让老话逐字不变(`test_the_search_wording_is_byte_for_byte_unchanged` 是金样)。
    """

    label: str  # 搜索 / 抓取
    verb: str  # 查不了 / 读不了


_SEARCH_WORDS = _Wording(label="搜索", verb="查不了")
_FETCH_WORDS = _Wording(label="抓取", verb="读不了")


class SearchPort(Protocol):
    """出网那一层的形状。**存在的理由只有一个:构造参数得有类型。**

    别把它当成插件体系的开端——没有注册表、没有配置项、没有按名字查找的地方。
    生产里的实现只有 `TavilySearch` 一个,测试里塞一个返回固定结果的假货,完。
    """

    def search(self, query: str, *, limit: int) -> list[WebResult]: ...


class FetchPort(Protocol):
    """读一个网页那一层的形状(M5-22)。和 `SearchPort` 分开:一个 Protocol 一件事,
    生产里恰好由同一个 key 接出两个客户端,但工具那边只依赖自己用得着的那一个。

    `deep` 是**兜底那一层的开关**,不是 Tavily 的词:`extract_depth` 这类服务商词汇
    只许出现在本文件里(D2)。上面那层只知道"再深一点试一次"。
    """

    def fetch(self, url: str, *, deep: bool) -> WebResult: ...


def parse_results(payload: Any) -> list[WebResult]:
    """把服务商的 JSON 收成 `WebResult` 列表。

    **报文是外部输入,形状一个都不许假设。** 缺字段、类型不对、整份不是 dict——
    一律收成空,不抛。一条畸形结果把整次搜索打成异常的话,真机上的面孔是"网络好着
    却查不了",而起居注里只有一个 KeyError,看不出是对面改了字段名。

    全空的条目跳掉:它渲染出来是一行「【(无标题)】 来源:(无来源链接)」,纯噪声。
    只缺一半的留着——有 url 就够用户自己点过去看。
    """
    results: list[WebResult] = []
    for item in _result_items(payload):
        hit = WebResult(
            title=_text_field(item, "title"),
            url=_text_field(item, "url"),
            text=_text_field(item, "content"),
        )
        if hit.title or hit.url or hit.text:
            results.append(hit)
    return results


def parse_extract(payload: Any, url: str) -> WebResult:
    """把 `/extract` 的报文收成**一页**。和 `parse_results` 同一条纪律:形状不许假设。

    **抽不出正文不是异常,是一页空的。** 服务商把打不开的 url 放进 `failed_results`,
    我们连看都不用看——空文本就是上面那层的判据:它靠"空或极短"决定升一次 advanced,
    再决定说哪一句话。在这里抛异常会把"我读不到这页"变成"网络出错了",两件事不一样,
    而用户按前者会去截图、按后者会去重启路由器。

    `url` 用**我们请求的那个**,不用报文里回来的那个:后者是服务商可以随便写的外部
    文本,而来源那一行的用处是让用户点回去核对——得是他自己给的那条链接。
    """
    for item in _result_items(payload):
        text = _text_field(item, "raw_content")
        if text:
            return WebResult(title=_text_field(item, "title"), url=url, text=text)
    return WebResult(title="", url=url, text="")


def _result_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


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
        payload = _post_json(
            self._endpoint,
            {"query": query, "max_results": limit, "search_depth": "basic"},
            api_key=self._api_key,
            timeout=self._timeout,
            transport=self._transport,
            words=_SEARCH_WORDS,
        )
        return parse_results(payload)


class TavilyExtract:
    """Tavily `/extract` 的适配:给一个 url,拿回一页 markdown(M5-22)。

    **和 `TavilySearch` 是同一条出网层的两个端点**,不是另起一套:同一个 key、同一个
    主机、同一份请求与异常映射(`_post_json`)。分成两个类只是因为一个 Protocol 一件事
    ——工具那边各自只依赖用得着的那一个。

    `deep=True` 就是 `extract_depth="advanced"`:**兜底那一层是一个参数,不是一个新
    系统。** 它换来的是"会渲染、且从别的 IP 出来"的取法——正因为如此,兜底也救不了
    403(渲染器得先能打开页面),那一类是住宅代理 + 浏览器指纹 + 过验证码的领域,不做。
    """

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = TAVILY_EXTRACT_ENDPOINT,
        timeout: float = EXTRACT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout = timeout
        self._transport = transport

    def fetch(self, url: str, *, deep: bool) -> WebResult:
        """读一页。**`url` 进的是 JSON body,永远不是请求地址**——出站目的地是
        `self._endpoint`,构造缺省写死。这就是"SSRF 面根本没有"的那一行代码。"""
        payload = _post_json(
            self._endpoint,
            {
                "urls": [url],
                "extract_depth": "advanced" if deep else "basic",
                "format": "markdown",
            },
            api_key=self._api_key,
            timeout=self._timeout,
            transport=self._transport,
            words=_FETCH_WORDS,
        )
        return parse_extract(payload, url)


def _post_json(
    endpoint: str,
    body: dict[str, Any],
    *,
    api_key: str,
    timeout: float,
    transport: httpx.BaseTransport | None,
    words: _Wording,
) -> Any:
    """两个端点共用的一发请求:鉴权、超时、状态码 → 人话、报文 → JSON。

    共用不是为了省行数,是为了**别有第二套异常映射**——漏掉一类的那个出口会把
    `httpx.ConnectError` 直接扔给模型,而那一轮当场死掉,用户看到的是助手崩了。
    """
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.post(
                endpoint, json=body, headers={"Authorization": f"Bearer {api_key}"}
            )
    except httpx.TimeoutException as exc:
        raise WebSearchError(f"{words.label}超时({timeout:.0f} 秒没回应),等一下再试。") from exc
    except httpx.HTTPError as exc:
        # 只带异常**类名**,不带 str(exc):后者会把 URL 原样带出来,而失败消息
        # 会被交给模型、落进起居注。类名足够分辨是连不上还是读坏了。
        raise WebSearchError(f"{words.label}没连上({type(exc).__name__}),网络可能不通。") from exc

    _raise_for_status(response.status_code, words)
    try:
        return response.json()
    except ValueError as exc:
        # 200 但正文是一页 HTML——网关插进来的错误页,真机上会遇到。
        raise WebSearchError(f"{words.label}服务回了看不懂的东西,这次{words.verb}。") from exc


def _raise_for_status(status: int, words: _Wording) -> None:
    """状态码 → 人话。**要分得清是哪一类**,三种的下一步动作完全不一样:
    401/403 是"你的 key 该重配了",429 是"这个月额度用完了,等等",5xx 是"对面的事"。
    笼统一句「搜索失败」会让用户去查错的东西。
    """
    if status in (401, 403):
        raise WebSearchError(f"{words.label}服务不认这个 key,要重新配 LARARIUM_TAVILY_KEY。")
    if status == 429:
        raise WebSearchError(f"{words.label}服务限流了,这个月的免费额度可能用完了,等一会儿再试。")
    if status >= 400:
        raise WebSearchError(f"{words.label}服务回了 {status},这次{words.verb},等一下再试。")
