from pathlib import Path

import pytest

from lararium.db import connect
from lararium.steward.journal import Journal
from lararium.steward.registry import Registry
from lararium.steward.tools import BuiltinTools


@pytest.fixture
def tools(tmp_path):
    journal = Journal(connect(tmp_path / "steward.sqlite"))
    return BuiltinTools(journal, Registry.load(Path("bundles")), timezone="Asia/Shanghai")


def test_current_time_returns_iso_with_configured_zone(tools):
    text = tools.current_time()
    assert "+08:00" in text


def test_read_skill_delegates_to_registry(tools):
    assert "三个判据" in tools.read_skill("memory", "writing-facts")


def test_read_skill_returns_readable_error_for_unknown(tools):
    """工具报错要让模型能自我纠正,不能抛异常炸掉整轮。"""
    result = tools.read_skill("finance", None)
    assert "没有这个 bundle" in result


def test_search_history_finds_chinese_and_formats_hits(tools):
    tools.journal.append("env-1", "envelope", {"content": "上周去了那家日料店"})
    result = tools.search_history("日料")
    assert "日料" in result
    assert "env-1" in result


def test_search_history_reports_no_match_clearly(tools):
    result = tools.search_history("完全不存在的内容")
    assert "没有找到" in result


def test_tool_function_order_is_fixed(tools):
    """工具 schema 顺序必须稳定,否则每次启动都毁前缀缓存。"""
    names = [f.__name__ for f in tools.as_tool_functions()]
    assert names == ["current_time", "read_skill", "search_history"]


async def test_search_history_works_from_a_worker_thread(tools):
    """同 Task 6:框架把同步工具丢线程池,search_history 会碰起居注的连接。"""
    import asyncio

    tools.journal.append("env-1", "envelope", {"content": "上周去了那家日料店"})
    result = await asyncio.to_thread(tools.search_history, "日料店")
    assert "日料店" in result
