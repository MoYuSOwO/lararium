"""M6-6a 学习 bundle 的七个工具:接口形状 + 每个工具的行为。

接口形状那几条是**机械检查**,不是靠 review 记得:工具签名里不许出现路径
——模型手里只有课程名,只有一个内部函数知道目录在哪(同 M6-5,同 M5-4 那条教训:
`Attachment` 上根本没有可写的 path 字段)。

课件(`add_file` / `list_materials` / `read_pdf`)是 M6-6b,这一轮一个字都没碰。
"""

import inspect
from pathlib import Path

import pytest
import yaml
from bundles.courses import store as st
from bundles.courses.server import build

SHANGHAI = "Asia/Shanghai"

# 一个工具的哪几个参数是**课程名**。表在这儿是为了让"加了第八个工具"必须过一次脑子:
# 下面第一条断言要求这张表和实现暴露的工具**一个不多一个不少**。
COURSE_NAME_PARAMS = {
    "list_courses": (),
    "read_note": ("course",),
    "append_to_note": ("course",),
    "replace_in_note": ("course",),
    "search_notes": ("course",),
    "rename_course": ("old", "new"),
    "delete_course": ("course",),
}

# 调一次工具要填的其余参数(和课程名无关的那些)。
FILLERS = {
    "text": "第一章:行列式按行展开",
    "old": "行列式",
    "new": "determinant",
    "reason": "记错课程名了",
    "query": "行列式",
}

BAD_NAMES = ["", "   ", "../../etc/passwd", "..", "a/b", "/etc/passwd", ".trash", "课" * 41]

NOTE = "# 线性代数\n\n## 第一章\n行列式 | 特征值\n<<< 围栏 >>> 🍅\r\n最后一行\n"


@pytest.fixture
def runtime(tmp_path):
    return build(tmp_path, timezone=SHANGHAI)


@pytest.fixture
def tools(runtime):
    return {f.__name__: f for f in runtime.tools}


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "courses"


def tree(root: Path) -> dict[str, bytes]:
    """整棵目录的快照(含 .trash)。断言"什么都没动"用它,别只看某一个文件。"""
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


# ── 接口形状:机械检查 ─────────────────────────────────────────────────


def test_the_course_name_table_covers_exactly_the_tools_that_exist(tools):
    assert set(COURSE_NAME_PARAMS) == set(tools)


def test_no_tool_signature_has_a_path_parameter(tools):
    """★ 验收口径:工具签名里不许有任何路径参数(同 M6-5 那条机械检查)。

    底下怎么落盘模型不知道——只有 `CourseStore.locate` 把课程名变成落盘位置,
    所以以后想改布局(比如 M6-6b 加 `materials/`)这七个签名一个字都不用动。
    """
    forbidden = ("path", "dir", "file", "folder", "root", "suffix")
    offenders = [
        f"{name}({param})"
        for name, fn in tools.items()
        for param in inspect.signature(fn).parameters
        for bad in forbidden
        if bad in param.lower()
    ]
    assert offenders == []


def test_every_tool_signature_is_frozen(tools):
    """工具 schema 是前缀第 0 层:签名定了就不许再动(A1)。断言全量,不断言片段。"""
    got = {name: list(inspect.signature(fn).parameters) for name, fn in tools.items()}
    assert got == {
        "list_courses": ["page", "include_deleted"],
        "read_note": ["course", "page"],
        "append_to_note": ["course", "text"],
        "replace_in_note": ["course", "old", "new"],
        "search_notes": ["query", "course", "page"],
        "rename_course": ["old", "new"],
        "delete_course": ["course", "reason", "undo"],
    }


def test_there_is_no_write_note_tool(tools):
    """★ **没有 `write_note`**(整篇覆盖一学期的笔记太危险);`append` + `replace` 够用,
    真要重构用户直接编那个 `.md`——那正是选文件而不是 SQLite 的理由。"""
    assert "write_note" not in tools


def test_materials_tools_are_not_here_yet(tools):
    """课件那一半是 M6-6b。**工具清单里不许出现没有实现的东西**
    ——能力边界写在清单里比写在错误信息里强。"""
    assert not {"add_file", "list_materials", "read_pdf"} & set(tools)


def test_manifest_declares_the_same_tools_in_the_same_order(runtime):
    """manifest 的顺序是设计时的权威,实现必须逐名对齐(顺序即冻结顺序)。"""
    manifest = yaml.safe_load(Path("bundles/courses/manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["tools"] == [f.__name__ for f in runtime.tools]


def test_every_tool_has_a_docstring(tools):
    """docstring 就是 schema:没有它,模型手里只有一个函数名。"""
    assert [n for n, f in tools.items() if not (f.__doc__ or "").strip()] == []


# ── 非法课程名:每个吃课程名的工具都得挡住,而且什么都不许动 ─────────────


@pytest.mark.parametrize("bad", BAD_NAMES, ids=repr)
def test_every_tool_refuses_an_illegal_course_name_and_changes_nothing(tools, root, bad):
    tools["append_to_note"]("线性代数", NOTE)
    before = tree(root)
    for name, fn in tools.items():
        for target in COURSE_NAME_PARAMS[name]:
            params = inspect.signature(fn).parameters
            kwargs = {p: FILLERS[p] for p in params if p in FILLERS}
            kwargs.update(dict.fromkeys(COURSE_NAME_PARAMS[name], "线性代数"))
            kwargs[target] = bad
            out = fn(**kwargs)
            assert isinstance(out, str) and out.strip(), f"{name}({target}={bad!r}) 没回人话"
            assert tree(root) == before, f"{name}({target}={bad!r}) 动了盘上的东西"


def test_an_unknown_course_gets_a_sentence_not_an_exception(tools):
    """不存在的课 → 一句人话,不抛(E2:抛了整轮炸掉,用户看到助手死掉)。"""
    for out in (
        tools["read_note"]("量子力学"),
        tools["replace_in_note"]("量子力学", "一", "二"),
        tools["search_notes"]("行列式", "量子力学"),
        tools["rename_course"]("量子力学", "量子物理"),
        tools["delete_course"]("量子力学", "不上了"),
    ):
        assert "量子力学" in out
        assert "没有" in out or "不存在" in out


def test_writing_through_a_symlinked_course_directory_is_refused(tools, root, tmp_path):
    """★ 名字合法、落点不合法:白名单挑不出「外链」的毛病,兜底按实际落点把它按住。"""
    outside = tmp_path / "外面"
    outside.mkdir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "外链").symlink_to(outside, target_is_directory=True)

    out = tools["append_to_note"]("外链", "不该写进去")
    assert list(outside.iterdir()) == []
    assert out.strip()


def test_a_notebook_that_is_not_utf8_becomes_a_sentence(tools, root):
    """用户自己的编辑器可能用别的编码存过某一本——那时候整轮不该死,而且要说出是哪门课。"""
    (root / "乱码课").mkdir(parents=True)
    (root / "乱码课" / st.NOTES).write_bytes("线性".encode("gbk"))
    out = tools["read_note"]("乱码课")
    assert "乱码课" in out and "UTF-8" in out
    assert "乱码课" in tools["search_notes"]("线性")


# ── append:隐式创建,而且**说出口** ────────────────────────────────────


def test_appending_to_a_new_course_says_it_created_one(tools):
    """★ 打错课程名得能救:打错的那次照样会创建,**说一声才挡得住**。

    「线性代数」打成「线性待数」就是一门新课,而加一个显式 `create_course` 挡不住
    (打错那次照样会填 True)——所以这句话是这条缺口的**唯一**防线。
    """
    out = tools["append_to_note"]("线性待数", "第一章")
    assert "新建" in out
    assert "线性待数" in out
    assert "rename_course" in out  # 顺手告诉它怎么救


def test_appending_to_an_existing_course_does_not_claim_to_create(tools):
    tools["append_to_note"]("线性代数", "第一章")
    out = tools["append_to_note"]("线性代数", "第二章")
    assert "新建" not in out


def test_three_appends_keep_everything_in_order(tools):
    tools["append_to_note"]("线性代数", NOTE)
    for text in ("第一次:行列式", "第二次:特征值", "第三次:对角化"):
        tools["append_to_note"]("线性代数", text)
    got = tools["read_note"]("线性代数")
    assert got.startswith(NOTE), "原有内容被动了"
    assert got.index("第一次") < got.index("第二次") < got.index("第三次")


def test_append_keeps_a_line_break_between_the_old_and_the_new(tools):
    tools["append_to_note"]("线性代数", "行列式")
    tools["append_to_note"]("线性代数", "特征值")
    assert tools["read_note"]("线性代数") == "行列式\n特征值"


def test_append_refuses_empty_text(tools, root):
    out = tools["append_to_note"]("线性代数", "  ")
    assert "空" in out
    assert not (root / "线性代数").exists(), "空 text 顺手建了一门课"


# ── read_note:逐字节 + 分页 ───────────────────────────────────────────


def test_write_then_read_is_byte_identical(tools):
    """★ 硬口径二:一页装得下的时候 `read_note` **什么都不加**,原样给回去。"""
    tools["append_to_note"]("线性代数", NOTE)
    assert tools["read_note"]("线性代数") == NOTE


def test_a_long_notebook_is_paged_and_says_so(tools):
    """★ 本子会长,所以必须分页;而**分页必须说出口**——静默截断读起来和"就这些"
    一模一样(M4-3)。页码钳位靠共用层的 `page_of`(0/负数/超大都不报错)。"""
    long_note = "".join(f"第 {i:03d} 行:行列式按行展开\n" for i in range(200))
    tools["append_to_note"]("线性代数", long_note)

    first = tools["read_note"]("线性代数")
    assert "第 1/" in first
    assert "search_notes" in first  # 长本子的正常姿势是搜
    bodies = []
    page = 1
    while True:
        out = tools["read_note"]("线性代数", page)
        head, _, body = out.partition("\n")
        assert f"第 {page}/" in head
        bodies.append(body)
        if f"第 {page}/{page}" in head:
            break
        page += 1
    assert "".join(bodies) == long_note, "拼回来不是原文"
    assert page > 1


@pytest.mark.parametrize("asked,want", [(0, "first"), (-7, "first"), (999, "last")])
def test_read_note_clamps_every_page_number(tools, asked, want):
    """0 / 负数钳到第一页,超大钳到**最后一页**而不是空——空的读起来和"没有了"一模一样。"""
    long_note = "".join(f"第 {i:03d} 行:行列式\n" for i in range(200))
    tools["append_to_note"]("线性代数", long_note)
    head = tools["read_note"]("线性代数", asked).partition("\n")[0]
    total = int(head.split("/")[1].split(" ")[0])
    assert total > 1
    assert f"第 {1 if want == 'first' else total}/{total} 页" in head, head


def test_reading_a_course_that_does_not_exist_says_how_to_start_one(tools):
    out = tools["read_note"]("量子力学")
    assert "量子力学" in out
    assert "append_to_note" in out


# ── replace:恰好一次才改 ──────────────────────────────────────────────


def test_replace_changes_the_only_match_and_shows_both_sides(tools):
    tools["append_to_note"]("线性代数", NOTE)
    out = tools["replace_in_note"]("线性代数", "行列式 | 特征值", "行列式 | 特征向量")
    assert "特征向量" in tools["read_note"]("线性代数")
    assert "改前" in out and "改后" in out


def test_replace_refuses_when_the_old_text_is_not_there(tools):
    tools["append_to_note"]("线性代数", NOTE)
    out = tools["replace_in_note"]("线性代数", "薛定谔方程", "算了")
    assert tools["read_note"]("线性代数") == NOTE
    assert "没找到" in out


def test_replace_refuses_when_the_old_text_appears_three_times(tools):
    """★ 多次命中要说出现了几次,并要求给更长的上下文;**绝不许改第一处**
    ——猜错了就是静默改坏用户一学期的笔记。"""
    text = "1. 行列式\n2. 行列式\n3. 行列式\n"
    tools["append_to_note"]("线性代数", text)
    out = tools["replace_in_note"]("线性代数", "行列式", "determinant")
    assert tools["read_note"]("线性代数") == text, "改了第一处 —— 这是静默改坏一本笔记"
    assert "3" in out


def test_replace_can_delete_a_passage(tools):
    tools["append_to_note"]("线性代数", "1. 一\n2. 记错了\n3. 三\n")
    tools["replace_in_note"]("线性代数", "2. 记错了\n", "")
    assert tools["read_note"]("线性代数") == "1. 一\n3. 三\n"


def test_replace_refuses_an_empty_old(tools):
    tools["append_to_note"]("线性代数", NOTE)
    out = tools["replace_in_note"]("线性代数", "", "x")
    assert tools["read_note"]("线性代数") == NOTE
    assert "空" in out


# ── list_courses ──────────────────────────────────────────────────────


def test_list_reports_the_total(tools):
    for name in ("线性代数", "高等数学", "大学物理"):
        tools["append_to_note"](name, "x")
    out = tools["list_courses"]()
    assert "3" in out
    for name in ("线性代数", "高等数学", "大学物理"):
        assert name in out


def test_list_says_something_human_when_there_is_nothing(tools):
    assert "append_to_note" in tools["list_courses"]()


def test_list_hides_deleted_courses_until_asked(tools):
    tools["append_to_note"]("线性代数", "x")
    tools["append_to_note"]("大学物理", "y")
    tools["delete_course"]("大学物理", "退课了")

    plain = tools["list_courses"]()
    assert "大学物理" not in plain
    assert "1" in plain

    with_deleted = tools["list_courses"](include_deleted=True)
    assert "大学物理" in with_deleted
    assert "已删" in with_deleted
    assert "退课了" in with_deleted


def test_list_never_shows_the_trash_as_a_course(tools):
    """★ `.trash` 自己不是一门课:名字白名单已经拒了前导 `.`,所以模型**寻址不到它**
    ——但列表要跳过它,否则用户会看到一门叫 `.trash` 的课,而她点不开。"""
    tools["append_to_note"]("线性代数", "x")
    tools["delete_course"]("线性代数", "退课了")
    assert st.TRASH_DIR not in tools["list_courses"](include_deleted=True)
    assert st.TRASH_DIR not in tools["list_courses"]()


def test_a_course_with_only_materials_is_still_a_course(tools, root):
    """M6-6b 的 `add_file` 会建出课程目录而还没有笔记——那也是一门课。"""
    (root / "大学物理" / st.MATERIALS).mkdir(parents=True)
    assert "大学物理" in tools["list_courses"]()


# ── search_notes:主要入口 ─────────────────────────────────────────────


def test_searching_across_courses_puts_the_course_name_on_every_line(tools):
    """★ 不给 course 就全搜,而**命中来自不同课,课程名必须出现在每一行**
    ——不然模型不知道那段在哪门课里,拿着片段没法接着读。

    **笔记正文里故意不写课程名**,断言也锚在**行首**:第一版的笔记以「# 线性代数」开头,
    于是变异「跨课搜索不标课程名」之后片段里照样有课程名,这条照绿——T6 第五种,
    锚点太弱(是变异检查逼出来的,判红的只剩排序那一条)。
    """
    tools["append_to_note"]("线性代数", "行列式按行展开\n")
    tools["append_to_note"]("高等数学", "行列式在这儿也提了一句\n")
    tools["append_to_note"]("大学物理", "牛顿第二定律\n")

    out = tools["search_notes"]("行列式")
    rows = [line for line in out.splitlines() if line.startswith("- ")]
    assert [r.removeprefix("- ").split("(")[0] for r in rows] == ["线性代数", "高等数学"], out
    assert "大学物理" not in out


def test_the_order_of_cross_course_hits_is_frozen_not_filesystem_order(tools):
    """★ 顺序**写死**:名字命中在前,同一类里按课程名排。跟着 `iterdir` 的顺序走的话,
    同一句搜索在两台机器上给不同的答案,而"第 1/2 页"就指着不同的东西。"""
    tools["append_to_note"]("高等数学", "行列式也提了\n")
    tools["append_to_note"]("大学物理", "行列式也提了\n")
    tools["append_to_note"]("行列式专题", "别的内容\n")

    rows = [
        line.split("(")[0].removeprefix("- ").strip()
        for line in tools["search_notes"]("行列式").splitlines()
        if line.startswith("- ")
    ]
    assert rows == ["行列式专题", "大学物理", "高等数学"]


def test_searching_inside_one_course_lists_several_places_with_page_numbers(tools):
    """★ 一门课一个笔记本,所以"在这本里搜"要给**好几处**,而且要说在第几页
    ——只给第一处的话,一本二十页的笔记等于搜不到。"""
    long_note = (
        "".join(f"第 {i:03d} 行:别的内容\n" for i in range(100))
        + "特征值第一次出现\n"
        + "".join(f"第 {i:03d} 行:别的内容\n" for i in range(100))
        + "特征值第二次出现\n"
    )
    tools["append_to_note"]("线性代数", long_note)

    out = tools["search_notes"]("特征值", "线性代数")
    rows = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(rows) == 2, out
    assert "2" in out.splitlines()[0]  # 命中几处说出口
    pages = {int(r.split("第 ")[1].split(" 页")[0]) for r in rows}
    assert len(pages) == 2, f"两处在不同页上,页码却一样:{out}"


def test_searching_inside_one_course_does_not_need_the_course_name_on_every_line(tools):
    """反过来:课程给定时每行再印一遍课程名是噪声——**问的是"在哪几处"**,
    不是"在哪门课"。两个问题不一样,答案的形状就不该一样(G7)。"""
    tools["append_to_note"]("线性代数", "特征值\n")
    rows = [
        line
        for line in tools["search_notes"]("特征值", "线性代数").splitlines()
        if line[:2] == "- "
    ]
    assert rows and all("线性代数" not in r for r in rows)


def test_search_says_something_human_when_nothing_matches(tools):
    tools["append_to_note"]("线性代数", "行列式\n")
    out = tools["search_notes"]("薛定谔")
    assert "list_courses" in out or "换个说法" in out


def test_search_refuses_an_empty_query(tools):
    tools["append_to_note"]("线性代数", "行列式\n")
    out = tools["search_notes"]("   ")
    assert "search_notes" in out
    assert not [line for line in out.splitlines() if line.startswith("- ")]


def test_search_never_reads_the_trash(tools):
    tools["append_to_note"]("大学物理", "牛顿第二定律\n")
    tools["delete_course"]("大学物理", "退课了")
    assert "大学物理" not in tools["search_notes"]("牛顿")


def test_search_sees_an_edit_made_behind_its_back(tools, root):
    """★ 不建索引的意义:用户用编辑器直接改了那本 `.md`,搜索立刻看得见。

    有索引的话这一刻就变味了,**而且变味之后没有任何报错**——搜不到的东西和
    "确实没写过"长得一模一样。
    """
    tools["append_to_note"]("线性代数", "# 线性代数\n")
    (root / "线性代数" / st.NOTES).write_text("# 线性代数\n若尔当标准型\n", encoding="utf-8")
    assert "线性代数" in tools["search_notes"]("若尔当标准型")


def test_search_pages_over_courses(tools):
    for i in range(25):
        tools["append_to_note"](f"课{i:02d}", "都写了行列式\n")
    first = tools["search_notes"]("行列式", None, 1)
    second = tools["search_notes"]("行列式", None, 2)
    assert "25" in first
    assert first != second
    assert tools["search_notes"]("行列式", None, 999) == tools["search_notes"]("行列式", None, 3)


# ── rename:打错了得能救 ───────────────────────────────────────────────


def test_rename_moves_the_notebook_and_the_materials_together(tools, root):
    """★ 改名搬的是**整个课程目录**,所以课件天然跟着走(M6-6b 的 `materials/`)。"""
    tools["append_to_note"]("线性待数", NOTE)
    (root / "线性待数" / st.MATERIALS).mkdir()
    (root / "线性待数" / st.MATERIALS / "第3讲.pdf").write_bytes(b"%PDF-1.4 fake")

    out = tools["rename_course"]("线性待数", "线性代数")

    assert tools["read_note"]("线性代数") == NOTE
    assert (root / "线性代数" / st.MATERIALS / "第3讲.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert not (root / "线性待数").exists()
    assert "线性代数" in out


def test_rename_refuses_when_the_new_course_already_exists(tools, root):
    """★ 这一条要钉:**合并比重名更坏——重名你看得见,合并是静默的。**

    拿一门有笔记也有课件的课钉死"合并没发生":两边的笔记各是各的、课件也没混。
    """
    tools["append_to_note"]("线性待数", "待数版\n")
    tools["append_to_note"]("线性代数", "代数版\n")
    (root / "线性待数" / st.MATERIALS).mkdir()
    (root / "线性待数" / st.MATERIALS / "甲.pdf").write_bytes(b"A")
    (root / "线性代数" / st.MATERIALS).mkdir()
    (root / "线性代数" / st.MATERIALS / "乙.pdf").write_bytes(b"B")

    out = tools["rename_course"]("线性待数", "线性代数")

    assert tools["read_note"]("线性待数") == "待数版\n"
    assert tools["read_note"]("线性代数") == "代数版\n"
    assert [p.name for p in (root / "线性待数" / st.MATERIALS).iterdir()] == ["甲.pdf"]
    assert [p.name for p in (root / "线性代数" / st.MATERIALS).iterdir()] == ["乙.pdf"]
    assert "已经有" in out


def test_rename_to_the_same_name_says_so_and_does_nothing(tools):
    tools["append_to_note"]("线性代数", NOTE)
    out = tools["rename_course"]("线性代数", " 线性代数 ")
    assert tools["read_note"]("线性代数") == NOTE
    assert "一样" in out or "没变" in out


# ── delete:整个目录搬到回收站,不是 unlink ─────────────────────────────


def test_delete_moves_the_whole_course_into_the_trash(tools, root):
    """★ M5-20 一个字不改:**移走不是 unlink**,笔记和课件一个字节不许销毁。"""
    tools["append_to_note"]("大学物理", NOTE)
    (root / "大学物理" / st.MATERIALS).mkdir()
    (root / "大学物理" / st.MATERIALS / "第3讲.pdf").write_bytes(b"%PDF-1.4 fake")

    out = tools["delete_course"]("大学物理", "退课了")

    assert not (root / "大学物理").exists()
    trashed = [p for p in sorted((root / st.TRASH_DIR).glob("大学物理-*")) if p.is_dir()]
    assert len(trashed) == 1
    assert (trashed[0] / st.NOTES).read_bytes() == NOTE.encode("utf-8")
    assert (trashed[0] / st.MATERIALS / "第3讲.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert "undo" in out


def test_undo_does_not_need_a_reason(tools):
    """★ **撤回是恢复路径,最不该有摩擦的就是它。**

    做菜那边 `reason` 原本是必填的位置参数,于是最自然的那一句
    `delete_recipe("红烧肉", undo=True)` 在工具边界上直接炸(缺参数),而套件里看不见
    ——三条 undo 测试**都编了一个理由传进去,把绕法固化了**(M6-5 验收补的那一条)。

    所以这里第一条 undo 测试就走**最自然的那句话**:`delete_course(course, undo=True)`。
    「删必须给理由」一点没放松,只是从 schema 的必填变成**工具自己拒**
    (下面 `..._needs_a_reason` 钉着),而工具拒得出一句人话、模型能照着补。
    """
    tools["append_to_note"]("大学物理", "牛顿第二定律\n")
    tools["delete_course"]("大学物理", "退课了")

    out = tools["delete_course"]("大学物理", undo=True)

    assert "拿回来" in out, out
    assert tools["read_note"]("大学物理") == "牛顿第二定律\n"


def test_undo_brings_everything_back_byte_for_byte(tools, root):
    """★ 验收口径(M5-20):撤回回来的笔记和课件**逐字节一致**。"""
    tools["append_to_note"]("大学物理", NOTE)
    (root / "大学物理" / st.MATERIALS).mkdir()
    (root / "大学物理" / st.MATERIALS / "第3讲.pdf").write_bytes(b"%PDF-1.4 fake")
    before = tree(root / "大学物理")

    tools["delete_course"]("大学物理", "退课了")
    tools["delete_course"]("大学物理", undo=True)

    assert tree(root / "大学物理") == before


def test_delete_needs_a_reason(tools):
    tools["append_to_note"]("大学物理", NOTE)
    out = tools["delete_course"]("大学物理", "   ")
    assert tools["read_note"]("大学物理") == NOTE
    assert "为什么" in out


def test_deleting_the_same_course_twice_keeps_both_reasons(tools):
    """回收站里是 `<课程>-<时间戳>`,所以同一门课删两次**两份都留着**
    ——做菜那边(没有时间戳)是拒绝第二次,两边都不销毁上一份。"""
    tools["append_to_note"]("大学物理", "第一版\n")
    tools["delete_course"]("大学物理", "第一次删")
    tools["append_to_note"]("大学物理", "第二版\n")
    tools["delete_course"]("大学物理", "第二次删")

    listed = tools["list_courses"](include_deleted=True)
    assert "第一次删" in listed and "第二次删" in listed
    assert tools["delete_course"]("大学物理", undo=True).count("大学物理") >= 1
    assert tools["read_note"]("大学物理") == "第二版\n", "撤回该拿回最近删的那一份"


def test_undo_when_nothing_was_deleted_says_so(tools):
    tools["append_to_note"]("大学物理", NOTE)
    out = tools["delete_course"]("大学物理", undo=True)
    assert tools["read_note"]("大学物理") == NOTE
    assert "没删" in out or "没有删" in out


def test_undo_refuses_when_a_live_course_holds_the_name(tools):
    tools["append_to_note"]("大学物理", "第一版\n")
    tools["delete_course"]("大学物理", "删了")
    tools["append_to_note"]("大学物理", "第二版\n")
    out = tools["delete_course"]("大学物理", undo=True)
    assert tools["read_note"]("大学物理") == "第二版\n"
    assert "已经有" in out or "占着" in out


# ── 一行一条里的换行 ──────────────────────────────────────────────────


def test_lines_that_are_one_per_row_fold_the_text_they_embed(tools):
    """**一行一条**的输出里,嵌进去的文本必须折行:不折的话一条删除理由就能凭换行
    伪造出后续列表项,而伪造出来的那行和真条目形式上一模一样(P1-2)。"""
    tools["append_to_note"]("大学物理", NOTE)
    tools["delete_course"]("大学物理", "退课了\n- 线性代数(已删:伪造的)")
    out = tools["list_courses"](include_deleted=True)
    assert len([line for line in out.splitlines() if line.startswith("- ")]) == 1
