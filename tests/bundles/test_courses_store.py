"""M6-6a 学习 bundle 的存储层:一门课一个目录、笔记本的分页、回收站搬整个目录。

共用的那一层(名字校验两道、`replace` 的匹配纪律、扫文件搜索、`page_of`)在
`lararium.docstore`,由 `tests/test_docstore.py` 和 `tests/bundles/test_recipes_store.py`
钉着,**这里不重测**。这里测的是学习这边独有的那些:

- **落点的单位是"一门课的目录"**,不是一个文件——所以改名和删除天然把 `materials/`
  一起带走(M6-6b 才建那个子目录,而这一轮要证"容得下");
- **笔记本会长**,所以有 `paginate`:做菜那边 `read_recipe` 不分页,这一条只有学习要,
  按 G5 留在这一边,不进共用层;
- **回收站里是 `<课程>-<时间戳>` 的目录**(M5-20 的形状),所以同一门课删两次不会撞,
  而课程名里可以有连字符,时间戳里不能有——名字得能原样解回来。
"""

import pytest
from bundles.courses import store as st
from bundles.courses.store import CourseStore

SHANGHAI = "Asia/Shanghai"

# 非法课程名那一组。判定在共用层,**这里问的是"学习这边每一条都说得出一句人话"**
# ——「白名单拿掉」那条变异靠它判红。
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
    "线代\x00",
    "带\x1b转义",
    "课" * 41,
]

TRICKY = "# 线性代数\n\n## 第一章\n行列式 | 特征值\n<<< 围栏 >>> 🍅\r\n最后一行\n"


@pytest.fixture
def store(tmp_path) -> CourseStore:
    return CourseStore(tmp_path / "courses", timezone=SHANGHAI)


# ── 落点:两道校验,单位是一门课的目录 ─────────────────────────────────


def test_a_course_gets_a_directory_with_the_notebook_inside_it(store):
    """★ 形状:`<root>/<课程>/notes.md`。**一门课一个笔记本**,不是一门课下面多份笔记
    ——多份会逼模型编文件名(「第一章笔记」/「chapter1」),别让它发明一个维度。"""
    spot = store.locate("线性代数")
    assert spot.folder == store.root / "线性代数"
    assert spot.notes == store.root / "线性代数" / st.NOTES


def test_name_is_normalized_before_validation(store):
    assert store.locate("  线性  代数 ").name == "线性 代数"
    assert store.locate("线性 代数").folder == store.locate(" 线性   代数  ").folder


@pytest.mark.parametrize("bad", BAD_NAMES, ids=repr)
def test_every_illegal_course_name_is_refused_with_a_sentence(store, bad):
    spot = store.locate(bad)
    assert spot.folder is None and spot.notes is None, f"{bad!r} 居然算出了落点"
    assert spot.error.strip(), f"{bad!r} 被拒了但没给人话"


@pytest.mark.parametrize("bad", BAD_NAMES, ids=repr)
def test_the_whitelist_alone_refuses_every_illegal_course_name(bad):
    """★ 落点兜底失效时,白名单还挡不挡——而且学习这边**每一条犯规都得有一句话**
    (判定共用,句子不共用)。"""
    assert st.name_error(st.normalize_name(bad)) is not None


def test_the_resolve_fallback_alone_still_blocks_a_symlinked_course(store, tmp_path):
    """★ 白名单失效时落点兜底还挡不挡:课程名完全合法,底下是一条指到目录外面的链接。

    「外链」三个字挑不出任何毛病,而没有这一道,`append_to_note("外链", ...)` 就是
    **透过那条链接往课程目录外面写**。这条是变异「落点兜底拿掉」的判据。
    """
    outside = tmp_path / "外面"
    outside.mkdir()
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "外链").symlink_to(outside, target_is_directory=True)

    assert st.name_error("外链") is None, "这个名字本身合法——白名单挑不出毛病"
    spot = store.locate("外链")
    assert spot.folder is None, "兜底没拦住指到目录外面的链接"
    assert spot.error.strip()


# ── 笔记本:一个字节不许变 ─────────────────────────────────────────────


def test_save_then_read_the_notebook_is_byte_identical(store):
    """★ 硬口径二:带换行、`|`、`<<<` `>>>`、CRLF、emoji,一个字节不许变。"""
    spot = store.locate("线性代数")
    store.save(spot.notes, TRICKY)
    assert store.read(spot.notes, label=spot.name) == TRICKY


def test_save_leaves_no_temp_file_behind(store):
    """原子替换用的 `.tmp` 不许留下来——留着它用户在课程目录里会看见。"""
    spot = store.locate("线性代数")
    store.save(spot.notes, TRICKY)
    assert [p.name for p in spot.folder.iterdir()] == [st.NOTES]


def test_a_course_directory_without_a_notebook_reads_as_empty(store):
    """M6-6b 的 `add_file` 会先建出课程目录、而那时还没有笔记——那是合法状态,
    读出来是空串,不抛。「这门课没有」由工具层看目录在不在来说(两句话不混成一句)。"""
    spot = store.locate("线性代数")
    spot.folder.mkdir(parents=True)
    assert store.read(spot.notes, label=spot.name) == ""


def test_a_notebook_that_is_not_utf8_says_which_course(store):
    """报错要说得出是哪门课:文件名一律叫 `notes.md`,「某处解码失败」指不到任何地方。"""
    spot = store.locate("乱码课")
    spot.notes.parent.mkdir(parents=True)
    spot.notes.write_bytes("线性".encode("gbk"))
    with pytest.raises(st.UnreadableNote, match="乱码课"):
        store.read(spot.notes, label=spot.name)


# ── 分页:切开再拼回来,一个字节不许变 ─────────────────────────────────


def test_pages_joined_back_together_are_the_original_text():
    """★ 分页只是"切开给",不是改写:拼回来必须逐字节等于原文。"""
    text = "".join(f"第 {i} 行 | 带 🍅 和 <<< 围栏 >>>\r\n" for i in range(200))
    pages = st.paginate(text, 300)
    assert len(pages) > 1
    assert "".join(pages) == text


def test_a_page_breaks_at_the_end_of_a_line():
    """在行尾切,不在字中间切——一段被切成两半的公式两边都读不懂。"""
    pages = st.paginate("一\n二\n三\n四\n", 4)
    assert pages[0] == "一\n二\n"


def test_a_single_line_longer_than_a_page_is_cut_hard():
    """一行就比一页长(用户粘了一大段没有换行的东西)时只能硬切,但**仍然拼得回去**。"""
    pages = st.paginate("x" * 25, 10)
    assert [len(p) for p in pages] == [10, 10, 5]
    assert "".join(pages) == "x" * 25


def test_an_empty_notebook_is_one_empty_page():
    assert st.paginate("", 10) == [""]


def test_page_index_says_which_page_an_offset_falls_on():
    """搜索命中的下标 → 第几页。没有这一步,搜到了也不知道去读哪一页
    ——而「找一段的正常姿势是搜」正是靠这一步闭合的。"""
    pages = st.paginate("一\n二\n三\n四\n", 4)  # ['一\n二\n', '三\n四\n']
    assert st.page_index(pages, 0) == 1
    assert st.page_index(pages, 4) == 2
    assert st.page_index(pages, 999) == 2  # 越界钳到最后一页,不报错


# ── 有哪些课:回收站不是一门课 ─────────────────────────────────────────


def test_names_lists_course_directories(store):
    for name in ("高等数学", "线性代数"):
        store.save(store.locate(name).notes, "x")
    # 码位序(线 U+7EBF < 高 U+9AD8),**不是拼音序**——要的是不抖,不是好看。
    assert store.names() == ["线性代数", "高等数学"]


def test_names_skips_the_trash_and_anything_else_the_model_cannot_address(store):
    """★ 名字白名单拒前导 `.`,所以**模型寻址不到**这些目录;列出来只会让用户看到一门
    叫 `.trash` 的课,而她点不开。这条是变异「list_courses 列出 .trash」的判据。"""
    store.save(store.locate("线性代数").notes, "x")
    (store.root / st.TRASH_DIR / "线性代数-20260101T000000").mkdir(parents=True)
    (store.root / ".git").mkdir()
    (store.root / "散落的文件.md").write_text("x", encoding="utf-8")
    assert store.names() == ["线性代数"]


# ── 回收站:整个目录搬走,课件跟着走 ───────────────────────────────────


def test_deleting_moves_the_whole_course_directory(store):
    """★ M5-20:移走不是 unlink,而学习这边搬的是**整个课程目录**
    ——所以 M6-6b 的 `materials/` 天然跟着走,这一轮拿一个手工放进去的课件钉住它。"""
    spot = store.locate("线性代数")
    store.save(spot.notes, TRICKY)
    (spot.folder / st.MATERIALS).mkdir()
    (spot.folder / st.MATERIALS / "第3讲.pdf").write_bytes(b"%PDF-1.4 fake")

    trashed = store.into_trash(spot.folder, spot.name, "退课了")

    assert not spot.folder.exists()
    assert (trashed / st.NOTES).read_bytes() == TRICKY.encode("utf-8")
    assert (trashed / st.MATERIALS / "第3讲.pdf").read_bytes() == b"%PDF-1.4 fake"


def test_deleting_the_same_course_twice_keeps_both_snapshots(store):
    """时间戳让同一门课删两次不会撞——而**同一秒里**删两次也不许把上一份盖掉
    (盖掉就是把上次删的那份销毁,而"删掉的还能拿回来"是这个工具的全部意义)。"""
    for reason in ("第一次", "第二次"):
        spot = store.locate("线性代数")
        store.save(spot.notes, reason)
        store.into_trash(spot.folder, spot.name, reason)
    assert [reason for _, reason, _ in store.deleted()] == ["第一次", "第二次"]


def test_a_course_name_with_a_hyphen_survives_the_trash_round_trip(store):
    """回收站目录叫 `<课程>-<时间戳>`,而课程名里可以有连字符。时间戳里**不能**有
    (所以是 `20260913T142233`):名字靠最后一个连字符解回来,不然「C-语言」会变成「C」。"""
    spot = store.locate("C-语言")
    store.save(spot.notes, "指针")
    store.into_trash(spot.folder, spot.name, "学完了")
    assert [name for name, _, _ in store.deleted()] == ["C-语言"]


def test_the_reason_lives_beside_the_trashed_course_not_inside_it(store):
    """理由不许落在被搬走的那个目录**里面**——那样恢复回来就多出一个文件,
    而 `undo` 的口径是**逐字节一致**。"""
    spot = store.locate("线性代数")
    store.save(spot.notes, "x")
    trashed = store.into_trash(spot.folder, spot.name, "退课了")
    assert sorted(p.name for p in trashed.iterdir()) == [st.NOTES]


def test_undo_brings_the_directory_back_and_drops_the_reason(store):
    spot = store.locate("线性代数")
    store.save(spot.notes, TRICKY)
    trashed = store.into_trash(spot.folder, spot.name, "退课了")

    store.out_of_trash(trashed, spot.folder)

    assert store.read(spot.notes, label=spot.name) == TRICKY
    assert store.deleted() == []
    assert not trashed.exists()


def test_a_symlinked_trash_is_refused_by_the_fallback(store, tmp_path):
    """★ G8:落点兜底不只在 `locate` 那两处——`.trash` 自己被做成一条指到外面的链接时,
    "搬进回收站"就是**把整门课搬出课程目录**。`locate` 只看课程目录,看不见这一条路。"""
    outside = tmp_path / "外面的回收站"
    outside.mkdir()
    spot = store.locate("线性代数")
    store.save(spot.notes, "x")
    (store.root / st.TRASH_DIR).symlink_to(outside, target_is_directory=True)

    assert store.into_trash(spot.folder, spot.name, "退课了") is None
    assert spot.folder.is_dir(), "兜底拦下了,课却已经搬走了"
    assert list(outside.iterdir()) == []
