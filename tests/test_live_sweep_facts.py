"""M5-23 验收:夜间归拢提上来的是**归纳过的事实**,不是用户的原话。**真模型。**

## 真机上发生了什么

31 轮对话,主对话 `propose` 了 0 次;跑一次归拢,提上来 3 条,**全是原话**——
「学校嘛 那天天上课睡觉 那不吃点好的怎么行」这种。而人工翻同样 31 轮找得出 4 条该入档的
(有女朋友、人在深圳、在上学、看 F1 但不是法拉利车迷),归拢**一条都没提**。

根因在 `prompts/sweep.md` 最后一句,它和同一份文件前面第 3 条自相矛盾:

    第 3 条    「关于用户的事实,提一条建议——三个月后大概还成立的那种」   ← 对
    最后一句   「suggest 里写用户的原话或它的直接表述,别加你的解读和推断」 ← 直接禁掉了归纳

**而模型听的是最后一句。** 账本要的恰恰是归纳:「主要是深圳就美食荒漠」→「人在深圳」。

## 这份装置是真机那 31 轮的**替身,不是那 31 轮本身**

真机的起居注在服务器上,我手里没有。所以这里按用户复述的口径重搭一段 31 轮的日常对话:
四条该入档的事实用**他实际说过的那种口语**埋进去(含他引用的两句原话),
再拌上一批**不该入档**的东西(当天心情、单次消费、一时之念)——只测"提得出来"不测
"不乱提",绿了也说明不了什么。

**真机复跑仍然是最终口径**,这条只是让它别带着已知的病上去。

跑法(默认跳过,不进日常门禁):

    set -a && source .env && set +a && uv run pytest tests/test_live_sweep_facts.py -v -s -m live
"""

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.live

# 一段普通的一天。**四条该入档的埋在口语里**,其余是噪声。
# 序号只为好数;真机那 31 轮的具体内容我没有,这里按用户复述的口径重搭。
CONVERSATION = [
    ("user", "早"),
    ("assistant", "早。今天有什么打算?"),
    ("user", "没啥 就上课 困死了"),
    ("user", "学校嘛 那天天上课睡觉 那不吃点好的怎么行"),
    ("assistant", "哈哈那中午想吃什么?"),
    ("user", "不知道 这边没啥好吃的"),
    ("user", "主要是深圳就美食荒漠"),
    ("assistant", "确实,深圳这点被吐槽挺多的。"),
    ("user", "我女朋友说想吃日料 但是有点贵"),
    ("assistant", "偶尔吃一次也还好。"),
    ("user", "算了 中午就食堂对付一下"),
    ("user", "食堂 12 块"),
    ("assistant", "记好了:餐饮 12.00 元。"),
    ("user", "下午没课 想睡个午觉"),
    ("assistant", "睡吧,补一觉下午精神点。"),
    ("user", "今天心情有点差 别问了"),
    ("assistant", "行,那不问。"),
    ("user", "晚上有比赛"),
    ("assistant", "什么比赛?"),
    ("user", "F1 啊 这周末有站"),
    ("user", "别以为我是法拉利粉 我可不是 我就是看热闹"),
    ("assistant", "懂了,纯看比赛不站队。"),
    ("user", "我女朋友不看这个 她嫌吵"),
    ("assistant", "那你自己看吧哈哈。"),
    ("user", "打车回宿舍 18"),
    ("assistant", "记好了:交通 18.00 元。"),
    ("user", "突然想吃红烧肉了"),
    ("assistant", "周末可以自己试试。"),
    ("user", "算了 太麻烦"),
    ("user", "睡了 明天还得早八"),
    ("assistant", "晚安。"),
]

# 人工翻那 31 轮找出来的四条。每条给一组关键词,命中任意一个算提到了——
# **判的是"提没提到这件事",不是"用词和我一样"**:钉字面就是在钉模型的措辞,
# 那种断言换个模型就红,而它红的不是这一步要防的东西。
EXPECTED = {
    "有女朋友": ("女朋友", "女友", "对象"),
    "人在深圳": ("深圳",),
    "在上学": ("上学", "学生", "读书", "在校", "上课", "学校"),
    "看 F1 但不是法拉利车迷": ("f1", "赛车", "法拉利"),
}

# 原话的指纹:提上来的条目里出现这些**长片段**,就说明它在抄而不是在归纳。
RAW_QUOTE_FINGERPRINTS = (
    "那不吃点好的怎么行",
    "美食荒漠",
    "别以为我是法拉利粉",
    "那天天上课睡觉",
)


def _load(journal, when: datetime) -> None:
    """把这段对话灌进起居注。**不走 31 次真模型**——这一步测的是归拢,不是主对话。"""
    for index, (role, text) in enumerate(CONVERSATION):
        stamp = (when + timedelta(minutes=index)).isoformat()
        if role == "user":
            journal.append(
                f"env-{index}",
                "envelope",
                {"content": text, "source": "user", "channel": "cli", "meta": {}, "ts": stamp},
            )
        else:
            journal.append(f"env-{index}", "reply", {"content": text})


async def test_sweep_proposes_normalised_facts_not_raw_quotes(live_steward, tmp_path):
    """★ 验收:提上来的是**归纳过的短句**,四条里至少三条被提到,而且没有一条是原话。"""
    from lararium.steward.registry import Registry
    from lararium.steward.sweep import make_sweeper

    now = datetime.now(UTC)
    _load(live_steward.journal, now - timedelta(hours=2))
    sweeper = make_sweeper(
        live_steward.settings,
        live_steward.journal,
        live_steward.threads,
        live_steward.gate,
        Registry.load(__import__("pathlib").Path("bundles")),
        ledger=live_steward.ledger,
    )

    # 上界要在**灌完之后**取:`journal.append` 用的是它自己的 `now()`,
    # 拿灌之前的时刻当 until,这段对话全部落在窗口外——归拢会扫到空,而"没提出东西"
    # 和"提得不对"在断言上长得一模一样(第一版就这么绿过一次,幸好是红的)。
    until = datetime.now(UTC) + timedelta(minutes=1)
    result = await sweeper.run(
        since=(now - timedelta(hours=24)).isoformat(), until=until.isoformat()
    )
    assert not result.skipped, f"根本没跑:{result.summary}"

    pending = [p.content for p in live_steward.gate.pending()]
    print(f"\n归拢:{result.summary}\n提上来 {len(pending)} 条:")
    for line in pending:
        print(f"  - {line}")

    blob = "\n".join(pending).lower()
    hit = [name for name, words in EXPECTED.items() if any(w in blob for w in words)]
    assert len(hit) >= 3, f"四条里只提到 {len(hit)} 条({hit});提上来的是:{pending}"

    for raw in RAW_QUOTE_FINGERPRINTS:
        assert raw not in blob, f"这是在抄原话,不是归纳:命中「{raw}」于 {pending}"
    longest = max(pending, key=len)
    assert len(longest) <= 30, f"条目太长,不像归纳出来的短句:「{longest}」"

    # 提的仍然是 pending,一条都不许直接进账本(单写者:Gate.settle)
    assert "深圳" not in live_steward.ledger.read(), "归拢把东西直接写进账本了"
