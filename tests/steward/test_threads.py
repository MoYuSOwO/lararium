"""话头存储测试(M3-2)。Steward 独占,和起居注同库同产权——不是 bundle。

话头每轮进上下文,不封顶会把信封撑爆:条数上限 MAX_OPEN=5、单条字数 MAX_NOTE_LEN=80。
"""

import pytest

from lararium.db import connect
from lararium.steward.threads import Threads


@pytest.fixture
def threads(tmp_path):
    return Threads(connect(tmp_path / "steward.sqlite"))


def test_open_creates_then_update_updates_same_topic(threads):
    """同名是更新不是新建。"""
    threads.open_thread("这次裁员", "小张被裁了,想帮他内推")
    threads.open_thread("这次裁员", "内推给了老赵,等消息")
    open_ones = threads.open_threads()
    assert len(open_ones) == 1, "同名应更新,不新建第二行"
    assert open_ones[0].topic == "这次裁员"
    assert open_ones[0].note == "内推给了老赵,等消息"


def test_close_removes_from_open(threads):
    threads.open_thread("买基金", "在等调仓")
    assert threads.close_thread("买基金") is True
    assert threads.open_threads() == []
    assert threads.close_thread("买基金") is False, "已关再关返回 False"


def test_open_threads_sorted_newest_first(threads):
    threads.open_thread("A", "先开")
    threads.open_thread("B", "后开")
    assert [t.topic for t in threads.open_threads()] == ["B", "A"]


def test_open_threads_caps_count_and_note_length(threads):
    for i in range(8):
        threads.open_thread(f"话题{i}", "用" * 20)
    topics = [t.topic for t in threads.open_threads()]
    assert len(topics) == 5, "条数上限 5"
    assert "话题7" in topics and "话题0" not in topics, "保留最近更新的,最旧的丢掉"

    threads.open_thread("长注记", "很" * 200)
    assert len(threads.open_threads()[0].note) == 80, "单条字数上限 80(就地截断)"


def test_open_thread_truncates_oversized_topic(threads):
    """topic 同样是模型传的、同样每轮进信封——必须和 note 一样就地截断。

    实测过不受限的 topic:open_thread("话"*5000, "短的") 后 5 条话头占 5086 字,
    本该是 80x5≈400。MAX_TOPIC_LEN 就是来堵这个的。
    """
    t = threads.open_thread("话" * 5000, "短的")
    assert len(t.topic) == 24  # MAX_TOPIC_LEN
    assert len(threads.open_threads()[0].topic) == 24


def test_topic_is_normalized_so_same_name_updates(threads):
    """「同名是更新」必须对真实用法成立:topic 要 strip(内部空白一并折叠)。

    实测没归一化时:("装修") / (" 装修") / ("装修 ") → 库里 3 条全露出来,
    close_thread(" 装修") 关掉的只是复制品。归一化后这些都该是同一把钥匙。
    """
    threads.open_thread(" 装修 ", "A")
    threads.open_thread("装修", "B")  # 首尾空白 strip 后同名 → 更新
    open_ones = threads.open_threads()
    assert len(open_ones) == 1, "首尾空白归一化后应同名更新,不新建"
    assert open_ones[0].note == "B"
    assert open_ones[0].topic == "装修"

    # close 用同一套归一化:存的和找的对得上
    assert threads.close_thread("  装修  ") is True
    assert threads.open_threads() == []


def test_empty_topic_is_rejected(threads):
    """空 topic 现在也能建一条(空串也是主键)——不该让它存在。"""
    try:
        threads.open_thread("   ", "空话题")
    except ValueError as exc:
        assert "话头" in str(exc)
    else:
        raise AssertionError("空 topic 应被拒绝")
    assert threads.open_threads() == []
