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
