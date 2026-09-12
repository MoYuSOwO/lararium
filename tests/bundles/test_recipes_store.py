"""M6-5 做菜 bundle 的存储层:名字校验那两道、落点、replace 的匹配纪律、扫文件搜索。

这一层是 M6-6(学习 bundle)要整体搬走的那一份,所以它单独有测试:搬过去之后这些断言
应该一条不改地跟着走。工具那一层的行为在 `test_recipes_tools.py`。
"""

import pytest
from bundles.recipes import store as st
from bundles.recipes.store import RecipeStore, UnreadableRecipe

# 非法名字那一组。**两道校验各自都要单独挡住它们的一部分**,见下面那两条变异级测试。
BAD_NAMES = [
    "",
    "   ",
    "../../etc/passwd",
    "..",
    "a/b",
    "a\\b",
    "/etc/passwd",
    ".trash",
    ".hidden",
    "番茄\x00炒蛋",
    "带\x1b转义",
    "菜" * 41,
]


@pytest.fixture
def store(tmp_path) -> RecipeStore:
    return RecipeStore(tmp_path / "recipes")


# ── 归一化:在校验之前 ─────────────────────────────────────────────────


def test_name_is_normalized_before_validation(store):
    """首尾空白、内部多空格,都对上同一道菜。"""
    assert store.locate("  番茄  炒鸡蛋 ").name == "番茄 炒鸡蛋"
    assert store.locate("番茄 炒鸡蛋").path == store.locate("  番茄   炒鸡蛋  ").path


def test_normalize_folds_newlines_and_tabs_too(store):
    assert st.normalize_name("番茄\n炒\t蛋") == "番茄 炒 蛋"


def test_a_carriage_return_in_a_name_is_folded_not_refused():
    """`\\r` 是空白,归一化在校验之前——所以它被折成空格,而不是撞上控制字符那一条。

    这条写下来是因为第一版把它列进了"非法名字"那一组,而判红说明它其实是合法的:
    两道校验看到的是**已经归一化过**的名字。控制字符那一条守的是 `\\x00` / `\\x1b`
    这类不是空白、也没有任何正当用途的字符。
    """
    assert st.normalize_name("带\r回车") == "带 回车"
    assert st.name_error("带 回车") is None


# ── 两道校验:各自单独成立 ─────────────────────────────────────────────


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_every_illegal_name_is_refused_with_a_sentence(store, bad):
    located = store.locate(bad)
    assert located.path is None, f"{bad!r} 居然算出了落点"
    assert located.error.strip(), f"{bad!r} 被拒了但没给人话"


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_the_whitelist_alone_refuses_every_illegal_name(bad):
    """★ 落点兜底失效时,白名单还挡不挡:逐条名字问 `name_error` 自己。

    变异检查里"把白名单拿掉"和"把兜底拿掉"各跑一次,靠的就是这两条分头断言。
    """
    assert st.name_error(st.normalize_name(bad)) is not None


def test_the_resolve_fallback_alone_still_blocks_an_escape(tmp_path):
    """★ 白名单失效时,落点兜底还挡不挡:绕过 `name_error` 直接算落点。

    兜底挡的是"出了这个目录",所以拿一个真能走出去的名字钉它——`..` 那一类。
    白名单里想漏的那些,最后都会在这一步按**实际落点**被按住。
    """
    root = (tmp_path / "recipes").resolve()
    assert st.under(root, root / "../../etc/passwd.md") is None
    assert st.under(root, root / "/etc/passwd.md") is None
    assert st.under(root, root / "番茄炒鸡蛋.md") is not None


def test_the_two_guards_cover_different_things(tmp_path):
    """两道不是一道的复制品:`.trash` 这种落点**在目录里面**,只有白名单拦得住。

    这条是"为什么要两道"的证据,不是风格声明:抽掉白名单之后,兜底对它无能为力。
    """
    root = (tmp_path / "recipes").resolve()
    assert st.under(root, root / ".trash/番茄.md") is not None  # 兜底放它过
    assert st.name_error(".trash/番茄") is not None  # 白名单按住它


def test_a_symlink_pointing_out_of_the_store_is_blocked_by_the_fallback(store, tmp_path):
    """★ 落点兜底**独有**的那一类:一个完全合法的菜名,底下是一条指到目录外面的符号链接。

    白名单对它无能为力(「外链」三个字挑不出任何毛病),而没有这一道的话,
    `write_recipe("外链", ...)` 就是**透过那条链接往目录外面写**。
    这条同时是变异检查"把落点兜底拿掉"那一条的判据——没有它,那次变异会静默全绿,
    而那正是 T6 第一种假绿(变异没造出可观测的 bug)。
    """
    outside = tmp_path / "外面.md"
    outside.write_text("不该被碰到", encoding="utf-8")
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "外链.md").symlink_to(outside)

    located = store.locate("外链")
    assert st.name_error("外链") is None, "这个名字本身合法——白名单挑不出毛病"
    assert located.path is None, "兜底没拦住指到目录外面的链接"
    assert located.error.strip()


def test_locate_gives_both_landing_spots_at_once(store):
    """一次 locate 给两个落点(菜谱里的、回收站里的):删除/恢复两边都要用,
    而"再 locate 一次拿另一个"会逼出一条永远走不到的错误分支。"""
    loc = store.locate("番茄炒鸡蛋")
    assert loc.path is not None and loc.trash is not None
    assert loc.trash.parent.name == st.TRASH_DIR
    assert loc.trash != loc.path


def test_an_illegal_name_yields_neither_landing_spot(store):
    loc = store.locate("../../etc/passwd")
    assert loc.path is None and loc.trash is None


# ── 存储:一个字节不许变 ───────────────────────────────────────────────

TRICKY = "# 番茄\n\n1. 一 | 二\n2. <<< 围栏 >>>\n\n经验:少放盐 🍅\t制表符\r\n回车\n"


def test_save_then_read_is_byte_identical(store):
    """★ 硬口径一:带换行、带 `|`、带围栏字符、带 emoji、带 CRLF,一个字节不许变。"""
    loc = store.locate("番茄炒鸡蛋")
    store.save(loc.path, TRICKY)
    assert store.read(loc.path) == TRICKY


def test_save_leaves_no_temp_file_behind(store):
    """原子替换用的 `.tmp` 不许留下来——留着它 `list_recipes` 不会列,但用户会看见。"""
    loc = store.locate("番茄炒鸡蛋")
    store.save(loc.path, TRICKY)
    assert [p.name for p in store.root.iterdir()] == ["番茄炒鸡蛋.md"]


def test_read_returns_none_for_a_dish_that_was_never_written(store):
    assert store.read(store.locate("红烧肉").path) is None


def test_a_file_that_is_not_utf8_says_which_dish(store):
    loc = store.locate("乱码菜")
    store.root.mkdir(parents=True, exist_ok=True)
    loc.path.write_bytes("番茄".encode("gbk"))
    with pytest.raises(UnreadableRecipe, match="乱码菜"):
        store.read(loc.path)


def test_move_does_not_rewrite_the_bytes(store):
    """改名 / 进回收站都是搬文件,所以字节天然一个不变。"""
    src, dst = store.locate("番茄炒鸡旦"), store.locate("番茄炒鸡蛋")
    store.save(src.path, TRICKY)
    store.move(src.path, dst.path)
    assert store.read(dst.path) == TRICKY
    assert store.read(src.path) is None


def test_the_reason_file_never_collides_when_the_name_has_a_dot(store):
    """菜名里可以有点,而两道点名菜的理由文件不许撞到同一个文件上。"""
    a = store.locate("蛋炒饭.简化版")
    b = store.locate("蛋炒饭.家常版")
    store.save(a.trash, "a")
    store.save(b.trash, "b")
    store.save_reason(a.trash, "理由甲")
    store.save_reason(b.trash, "理由乙")
    assert dict(store.deleted()) == {"蛋炒饭.简化版": "理由甲", "蛋炒饭.家常版": "理由乙"}


# ── 列名字:回收站不在里面 ─────────────────────────────────────────────


def test_names_skips_the_trash(store):
    """★ `glob` 不是 `rglob`:删掉的那份不许出现在「存了哪些」里。"""
    live = store.locate("番茄炒鸡蛋")
    gone = store.locate("红烧肉")
    store.save(live.path, "x")
    store.save(gone.trash, "y")
    assert store.names() == ["番茄炒鸡蛋"]
    assert store.deleted() == [("红烧肉", "")]


def test_entries_also_skips_the_trash(store):
    """搜索扫的是活着的那些——删掉的菜不该被搜出来。"""
    store.save(store.locate("番茄炒鸡蛋").path, "要放糖")
    store.save(store.locate("红烧肉").trash, "要放糖")
    assert store.entries() == [("番茄炒鸡蛋", "要放糖")]


# ── replace 的匹配纪律 ────────────────────────────────────────────────


def test_replace_once_changes_the_only_match():
    got = st.replace_once("盐一勺,糖一勺", "盐一勺", "盐半勺")
    assert got.count == 1
    assert got.text == "盐半勺,糖一勺"


def test_replace_once_refuses_when_there_is_no_match():
    got = st.replace_once("盐一勺", "酱油一勺", "酱油半勺")
    assert got.count == 0
    assert got.text is None


def test_replace_once_never_touches_the_first_of_several():
    """★ 多次命中**绝不允许"改第一处"**——猜错了就是静默改坏一份做法。"""
    text = "加盐\n加盐\n加盐"
    got = st.replace_once(text, "加盐", "加糖")
    assert got.count == 3
    assert got.text is None


def test_replace_once_allows_deleting_a_passage():
    got = st.replace_once("一\n要放味精\n二", "要放味精\n", "")
    assert got.count == 1
    assert got.text == "一\n二"


def test_excerpt_folds_and_marks_both_ends():
    text = "0123456789\nabcdefghij\n0123456789"
    got = st.excerpt(text, text.index("abc"), 3, radius=4)
    assert got == "…789 abcdefg…"


def test_excerpt_does_not_mark_an_end_it_did_not_cut():
    assert st.excerpt("abcdef", 0, 2, radius=99) == "abcdef"


# ── 渲染那一刀 ────────────────────────────────────────────────────────


def test_one_line_folds_every_kind_of_whitespace():
    assert st.one_line("一\n二\t三\r\n四  五") == "一 二 三 四 五"


def test_one_line_neutralizes_the_inline_delimiters():
    """一行里的界符要中和:不然正文能在同一行伪造出第二个字段。"""
    got = st.one_line("理由「假的」")
    assert st.OPEN not in got
    assert st.CLOSE not in got


def test_clip_says_how_much_it_dropped():
    got = st.clip("一二三四五", 2)
    assert got.startswith("一二")
    assert "3" in got


def test_clip_leaves_a_short_text_alone():
    assert st.clip("一二", 5) == "一二"


# ── 搜索:两样都搜、标出是哪一种 ───────────────────────────────────────

ENTRIES = [
    ("番茄炒鸡蛋", "番茄两个,少放酱油"),
    ("红烧肉", "酱油两勺,冰糖一块"),
    ("凉面", "煮面水留一点"),
]


def test_scan_finds_a_hit_in_the_content():
    hits = st.scan(ENTRIES, "冰糖")
    assert [h.name for h in hits] == ["红烧肉"]
    assert hits[0].in_text and not hits[0].in_name
    assert "冰糖" in hits[0].snippet


def test_scan_finds_a_hit_in_the_name():
    hits = st.scan(ENTRIES, "凉面")
    assert [h.name for h in hits] == ["凉面"]
    assert hits[0].in_name


def test_scan_marks_a_dish_that_hits_both_ways():
    hits = st.scan(ENTRIES, "番茄")
    assert [(h.name, h.in_name, h.in_text) for h in hits] == [("番茄炒鸡蛋", True, True)]


def test_scan_puts_name_hits_first():
    """名字命中通常更强,排前面;同一类里按菜名定序,输出不许抖。"""
    entries = [("酱油鸡", "白切"), ("红烧肉", "酱油两勺"), ("卤蛋", "酱油一碗")]
    assert [h.name for h in st.scan(entries, "酱油")] == ["酱油鸡", "卤蛋", "红烧肉"]


def test_scan_returns_a_fragment_not_the_whole_file():
    """片段不是"全部读出来":一份长做法只回命中处附近那一小段。"""
    long_text = "前" * 500 + "关键字" + "后" * 500
    hits = st.scan([("长做法", long_text)], "关键字")
    assert len(hits[0].snippet) < 2 * st.SNIPPET_RADIUS + 20
    assert "关键字" in hits[0].snippet


def test_scan_ignores_ascii_case():
    assert [h.name for h in st.scan([("Pasta", "Al Dente")], "dente")] == ["Pasta"]


def test_scan_returns_nothing_when_nothing_matches():
    assert st.scan(ENTRIES, "佛跳墙") == []


# ── 分页:0/负数/超大都钳住 ────────────────────────────────────────────


@pytest.mark.parametrize("asked,expected", [(0, 1), (-5, 1), (1, 1), (2, 2), (999, 3)])
def test_page_of_clamps_the_page_number(asked, expected):
    items = list(range(25))
    _, page, total_pages = st.page_of(items, asked, 10)
    assert (page, total_pages) == (expected, 3)


def test_page_of_slices_the_asked_page():
    items = list(range(25))
    assert st.page_of(items, 3, 10)[0] == [20, 21, 22, 23, 24]


def test_page_of_reports_one_page_when_there_is_nothing():
    assert st.page_of([], 1, 10) == ([], 1, 1)
