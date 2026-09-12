"""M6-6a 共用层(`lararium.docstore`)里**两个 bundle 都靠、而工具层看不见**的那几样。

这个文件**不重测**已经被 `tests/bundles/test_recipes_store.py` 钉住的那一层
——那份测试是 M6-5 写的,这一轮一条断言都没动,而它现在测的就是这个共用层
(经由 `bundles.recipes.store` 的转出)。**它一条不改还全绿,本身就是"这次是提取
不是重写"的证据**,所以不搬、不抄。

这里只补两样它看不见的:
1. `find_all` —— 这一轮**新的**那一个函数(`scan` 现在拿它的第一个落点,而学习那边要拿
   一本长笔记里的**好几处**)。它是"怎么算命中"的唯一出处,两边的大小写/重叠/上限口径
   都从它来。
2. `name_fault` 的**判定**本身 —— 工具层只看得见人话,而人话是各 bundle 自己的;
   共用的是"犯了哪一条",而两个 bundle 的十几句话全挂在这个判定上。
"""

import pytest

from lararium import docstore as ds

# ── find_all:唯一一份"怎么算命中" ─────────────────────────────────────


def test_find_all_returns_every_occurrence_in_order():
    assert ds.find_all("加盐\n加糖\n加盐", "加盐", limit=10) == [0, 6]


def test_find_all_stops_at_the_limit():
    """`scan` 拿 limit=1(一份一条),笔记本那边拿多条——**同一个函数,差一个数**。"""
    assert ds.find_all("aaaa", "a", limit=2) == [0, 1]


def test_find_all_does_not_overlap():
    """`aa` 在 `aaaa` 里算 2 处,不是 3 处——和 `str.count` 同一个口径,
    所以"命中几处"那个数和列出来的行数对得上(T6 第四种:两个口径要对账)。"""
    assert ds.find_all("aaaa", "aa", limit=10) == [0, 2]
    assert "aaaa".count("aa") == 2


def test_find_all_ignores_ascii_case():
    assert ds.find_all("Al Dente", "dente", limit=10) == [3]


def test_find_all_of_an_empty_query_finds_nothing():
    """空词不许命中"每一个位置"——那会让一次空搜索列出整本笔记。"""
    assert ds.find_all("随便", "", limit=10) == []


def test_scan_reports_where_the_first_content_hit_is():
    """`Hit.at` 是学习那边换算"在第几页"的全部依据;只命中名字时它是 -1。"""
    hits = ds.scan([("线性代数", "第三章 行列式")], "行列式")
    assert hits[0].at == 4
    assert ds.scan([("行列式", "别的")], "行列式")[0].at == -1


# ── name_fault:判定共用,人话不共用 ───────────────────────────────────

FAULTS = {
    "": "empty",
    "菜" * 41: "too_long",
    "a/b": "forbidden",
    "a\\b": "forbidden",
    "..": "forbidden",
    ".hidden": "leading_dot",
    "带\x00空": "control",
}


@pytest.mark.parametrize("name,kind", FAULTS.items(), ids=repr)
def test_name_fault_names_which_rule_was_broken(name, kind):
    fault = ds.name_fault(name)
    assert fault is not None and fault.kind == kind


def test_name_fault_reports_which_separator_it_hit():
    """★ 撞上的那个字符要带出来:两个 bundle 的句子里都印着它
    (「菜名里不能有「/」」),而 `../../etc/passwd` 撞上的是「/」不是「..」
    ——`_FORBIDDEN` 的顺序因此是接口的一部分。"""
    assert ds.name_fault("../../etc/passwd").found == "/"
    assert ds.name_fault("..").found == ".."


def test_a_normal_name_has_no_fault():
    assert ds.name_fault("线性代数") is None
    assert ds.name_fault("C-语言") is None  # 名字里可以有连字符(回收站的时间戳靠它分段)
