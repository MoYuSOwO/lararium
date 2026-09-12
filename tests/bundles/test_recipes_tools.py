"""M6-5 做菜 bundle 的八个工具:接口形状 + 每个工具的行为。

接口形状那几条是**机械检查**,不是靠 review 记得:工具签名里不许出现路径、不许出现
category(用户定了扁平化:「番茄炒鸡蛋属于什么类?」),而这是设计约束不是风格偏好。
"""

import inspect
from pathlib import Path

import pytest
import yaml
from bundles.recipes import store as st
from bundles.recipes.server import build

# 一个工具的哪几个参数是**菜名**。表在这儿是为了让"加了第九个工具"必须过一次脑子:
# 下面第一条断言要求这张表和实现暴露的工具**一个不多一个不少**。
DISH_NAME_PARAMS = {
    "list_recipes": (),
    "read_recipe": ("name",),
    "write_recipe": ("name",),
    "append_to_recipe": ("name",),
    "replace_in_recipe": ("name",),
    "search_recipes": (),
    "rename_recipe": ("old", "new"),
    "delete_recipe": ("name",),
}

# 调一次工具要填的其余参数(和菜名无关的那些)。
FILLERS = {
    "content": "# 番茄炒鸡蛋\n1. 打蛋",
    "text": "这次少放了盐,好吃",
    "old": "1. 打蛋",
    "new": "1. 把蛋打散",
    "reason": "记错名字了",
    "query": "番茄",
}

BAD_NAMES = ["", "   ", "../../etc/passwd", "..", "a/b", "/etc/passwd", ".trash", "菜" * 41]

RECIPE = "# 番茄炒鸡蛋\n\n## 做法\n1. 烫番茄 | 去皮\n2. 打蛋 <<< 注意 >>>\n3. 一起炒 🍅\n"


@pytest.fixture
def runtime(tmp_path):
    return build(tmp_path)


@pytest.fixture
def tools(runtime):
    return {f.__name__: f for f in runtime.tools}


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "recipes"


def tree(root: Path) -> dict[str, bytes]:
    """整棵目录的快照(含 .trash)。断言"什么都没动"用它,别只看某一个文件。"""
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


# ── 接口形状:机械检查 ─────────────────────────────────────────────────


def test_the_dish_name_table_covers_exactly_the_tools_that_exist(tools):
    assert set(DISH_NAME_PARAMS) == set(tools)


def test_no_tool_signature_has_a_path_or_category_parameter(tools):
    """★ 验收口径:工具签名里不许有任何路径参数,也不许有 category。

    底下怎么落盘模型不知道——只有一个内部函数(`RecipeStore.locate`)把菜名变成落盘
    位置,所以以后想改布局、改成别处存,这八个签名一个字都不用动。这条是**机械的**:
    靠 review 记得的规则,第九个工具那天就会破。
    """
    # 拦的是**寻址**参数。第一版还列了 "ext",结果把 `append_to_recipe(text)` 判成了
    # 违规——"text" 是要追加的正文,和落盘位置毫无关系。判红判对了:这条规则守的是
    # "模型不许自己指定东西存在哪",不是"参数名里不许出现这三个字母"。
    forbidden = ("path", "dir", "file", "folder", "category", "root", "suffix")
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
        "list_recipes": ["page", "include_deleted"],
        "read_recipe": ["name"],
        "write_recipe": ["name", "content"],
        "append_to_recipe": ["name", "text"],
        "replace_in_recipe": ["name", "old", "new"],
        "search_recipes": ["query", "page"],
        "rename_recipe": ["old", "new"],
        "delete_recipe": ["name", "reason", "undo"],
    }


def test_manifest_declares_the_same_tools_in_the_same_order(runtime):
    """manifest 的顺序是设计时的权威,实现必须逐名对齐(顺序即冻结顺序)。"""
    manifest = yaml.safe_load(Path("bundles/recipes/manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["tools"] == [f.__name__ for f in runtime.tools]


def test_every_tool_has_a_docstring(tools):
    """docstring 就是 schema:没有它,模型手里只有一个函数名。"""
    assert [n for n, f in tools.items() if not (f.__doc__ or "").strip()] == []


# ── 非法名字:每个吃菜名的工具都得挡住,而且什么都不许动 ───────────────


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_every_tool_refuses_an_illegal_dish_name_and_changes_nothing(tools, root, bad):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    before = tree(root)
    for name, fn in tools.items():
        for target in DISH_NAME_PARAMS[name]:
            params = inspect.signature(fn).parameters
            kwargs = {p: FILLERS[p] for p in params if p in FILLERS}
            kwargs.update(dict.fromkeys(DISH_NAME_PARAMS[name], "番茄炒鸡蛋"))
            kwargs[target] = bad
            out = fn(**kwargs)
            assert isinstance(out, str) and out.strip(), f"{name}({target}={bad!r}) 没回人话"
            assert tree(root) == before, f"{name}({target}={bad!r}) 动了盘上的东西"


def test_an_unknown_dish_gets_a_sentence_not_an_exception(tools):
    """不存在的菜名 → 一句人话,不抛(E2:抛了整轮炸掉,用户看到助手死掉)。"""
    for out in (
        tools["read_recipe"]("佛跳墙"),
        tools["append_to_recipe"]("佛跳墙", "一段"),
        tools["replace_in_recipe"]("佛跳墙", "一", "二"),
        tools["rename_recipe"]("佛跳墙", "佛跳墙2"),
        tools["delete_recipe"]("佛跳墙", "不要了"),
    ):
        assert "佛跳墙" in out
        assert "没有" in out or "不存在" in out


def test_writing_through_a_symlink_out_of_the_store_is_refused(tools, root, tmp_path):
    """★ 名字合法、落点不合法:白名单挑不出「外链」的毛病,兜底按实际落点把它按住。

    没有这一道,`write_recipe("外链", ...)` 就是透过那条链接往菜谱目录外面写。
    """
    outside = tmp_path / "外面.md"
    outside.write_text("不该被碰到", encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    (root / "外链.md").symlink_to(outside)

    out = tools["write_recipe"]("外链", "覆盖你")
    assert outside.read_text(encoding="utf-8") == "不该被碰到"
    assert out.strip()


def test_a_file_that_is_not_utf8_becomes_a_sentence(tools, root):
    """用户自己的编辑器可能用别的编码存过某一份——那时候整轮不该死。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "乱码菜.md").write_bytes("番茄".encode("gbk"))
    out = tools["read_recipe"]("乱码菜")
    assert "乱码菜" in out and "UTF-8" in out
    assert "乱码菜" in tools["search_recipes"]("番茄")


# ── write:逐字节一致,以及"之前没有这道,给你新建了" ────────────────────


def test_write_then_read_is_byte_identical(tools):
    """★ 硬口径一:带换行、带 `|`、带 `<<<` / `>>>`、带 emoji,一个字节不许变。"""
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    assert tools["read_recipe"]("番茄炒鸡蛋") == RECIPE


def test_write_to_a_new_dish_says_it_created_one(tools):
    """★ 打错菜名得能救:打错的那次照样会创建,**说一声才挡得住**(显式 create 挡不住)。"""
    out = tools["write_recipe"]("番茄炒鸡旦", RECIPE)
    assert "新建" in out
    assert "番茄炒鸡旦" in out
    assert "rename_recipe" in out  # 顺手告诉它怎么救


def test_write_to_an_existing_dish_updates_and_does_not_claim_to_create(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    out = tools["write_recipe"]("番茄炒鸡蛋", "# 新版\n1. 变了\n")
    assert "新建" not in out
    assert "覆盖" in out
    assert tools["read_recipe"]("番茄炒鸡蛋") == "# 新版\n1. 变了\n"


def test_writing_the_same_name_twice_is_one_dish_not_two(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    tools["write_recipe"]("  番茄炒鸡蛋 ", RECIPE)
    assert tools["list_recipes"]().count("番茄炒鸡蛋") == 1


def test_a_name_with_odd_spacing_finds_the_same_dish(tools):
    """名字首尾空白 / 内部多空格 → 和原名对上同一道菜。"""
    tools["write_recipe"]("番茄 炒鸡蛋", RECIPE)
    assert tools["read_recipe"]("  番茄   炒鸡蛋  ") == RECIPE


def test_write_refuses_empty_content_instead_of_wiping_the_dish(tools):
    """整篇覆盖不留上一版,所以空 content 会**无声清掉**一份做法——拦下来说一句。"""
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    out = tools["write_recipe"]("番茄炒鸡蛋", "   ")
    assert tools["read_recipe"]("番茄炒鸡蛋") == RECIPE
    assert "空" in out


# ── append:三段都在,顺序不变,原有内容一字没动 ────────────────────────


def test_three_appends_keep_everything_in_order(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    for text in ("第一次:咸了", "第二次:盐减半,行", "第三次:加了糖,更好"):
        tools["append_to_recipe"]("番茄炒鸡蛋", text)
    got = tools["read_recipe"]("番茄炒鸡蛋")
    assert got.startswith(RECIPE), "原有内容被动了"
    assert got.index("第一次") < got.index("第二次") < got.index("第三次")


def test_append_keeps_a_line_break_between_the_old_and_the_new(tools):
    """原文没以换行结尾时补一个,否则追加的那句会黏在最后一行上。"""
    tools["write_recipe"]("番茄炒鸡蛋", "1. 打蛋")
    tools["append_to_recipe"]("番茄炒鸡蛋", "经验:少放盐")
    assert tools["read_recipe"]("番茄炒鸡蛋") == "1. 打蛋\n经验:少放盐"


def test_append_does_not_create_a_dish(tools, root):
    """隐式创建只留给 write_recipe 一条路——两条路就是两次"打错字创建新菜"。"""
    out = tools["append_to_recipe"]("番茄炒鸡旦", "一段")
    assert "write_recipe" in out
    assert not (root / "番茄炒鸡旦.md").exists()


def test_append_refuses_empty_text(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    out = tools["append_to_recipe"]("番茄炒鸡蛋", "  ")
    assert tools["read_recipe"]("番茄炒鸡蛋") == RECIPE
    assert "空" in out


# ── replace:恰好一次才改 ──────────────────────────────────────────────


def test_replace_changes_the_only_match_and_shows_both_sides(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    out = tools["replace_in_recipe"]("番茄炒鸡蛋", "2. 打蛋", "2. 把蛋打散")
    assert "把蛋打散" in tools["read_recipe"]("番茄炒鸡蛋")
    assert "改前" in out and "改后" in out
    assert "2. 打蛋" in out and "2. 把蛋打散" in out


def test_replace_refuses_when_the_old_text_is_not_there(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    out = tools["replace_in_recipe"]("番茄炒鸡蛋", "先炸个鸡腿", "算了")
    assert tools["read_recipe"]("番茄炒鸡蛋") == RECIPE
    assert "没找到" in out


def test_replace_refuses_when_the_old_text_appears_three_times(tools):
    """★ 多次命中要说出现了几次,并要求给更长的上下文;**绝不许改第一处**。"""
    text = "1. 加盐\n2. 加盐\n3. 加盐\n"
    tools["write_recipe"]("腌肉", text)
    out = tools["replace_in_recipe"]("腌肉", "加盐", "加糖")
    assert tools["read_recipe"]("腌肉") == text, "改了第一处 —— 这是静默改坏一份做法"
    assert "3" in out


def test_replace_can_delete_a_passage(tools):
    tools["write_recipe"]("番茄炒鸡蛋", "1. 一\n2. 味精一勺\n3. 三\n")
    tools["replace_in_recipe"]("番茄炒鸡蛋", "2. 味精一勺\n", "")
    assert tools["read_recipe"]("番茄炒鸡蛋") == "1. 一\n3. 三\n"


def test_replace_refuses_an_empty_old(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    out = tools["replace_in_recipe"]("番茄炒鸡蛋", "", "x")
    assert tools["read_recipe"]("番茄炒鸡蛋") == RECIPE
    assert "空" in out


# ── list_recipes ──────────────────────────────────────────────────────


def test_list_reports_the_total(tools):
    for name in ("番茄炒鸡蛋", "红烧肉", "凉面"):
        tools["write_recipe"](name, RECIPE)
    out = tools["list_recipes"]()
    assert "3" in out
    for name in ("番茄炒鸡蛋", "红烧肉", "凉面"):
        assert name in out


def test_list_says_something_human_when_there_is_nothing(tools):
    out = tools["list_recipes"]()
    assert "write_recipe" in out


@pytest.mark.parametrize("page", [0, -7, 1, 999])
def test_list_clamps_every_page_number(tools, page):
    for i in range(3):
        tools["write_recipe"](f"菜{i}", RECIPE)
    out = tools["list_recipes"](page=page)
    assert "3" in out
    assert "菜0" in out  # 3 道菜一页装得下,任何页码都该回这一页


def test_list_hides_deleted_dishes_until_asked(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    tools["write_recipe"]("红烧肉", RECIPE)
    tools["delete_recipe"]("红烧肉", "做不好,不要了")
    plain = tools["list_recipes"]()
    assert "红烧肉" not in plain
    assert "1" in plain
    with_deleted = tools["list_recipes"](include_deleted=True)
    assert "红烧肉" in with_deleted
    assert "已删" in with_deleted
    assert "做不好" in with_deleted


def test_list_never_leaks_the_trash_directory_itself(tools):
    tools["write_recipe"]("红烧肉", RECIPE)
    tools["delete_recipe"]("红烧肉", "不要了")
    assert st.TRASH_DIR not in tools["list_recipes"](include_deleted=True)


# ── search:两样都搜、标出哪一种、只回片段 ──────────────────────────────


def test_search_finds_a_dish_by_its_content(tools):
    """真正的用处:「我之前哪道菜写了要少放酱油」——列名字一点用没有。"""
    tools["write_recipe"]("番茄炒鸡蛋", "# 番茄炒鸡蛋\n少放酱油,不然发黑\n")
    tools["write_recipe"]("红烧肉", "# 红烧肉\n冰糖一块\n")
    out = tools["search_recipes"]("酱油")
    assert "番茄炒鸡蛋" in out
    assert "红烧肉" not in out
    assert "酱油" in out


def test_search_marks_whether_the_hit_was_the_name_or_the_content(tools):
    tools["write_recipe"]("凉面", "煮面水留一点\n")
    tools["write_recipe"]("红烧肉", "别用凉面配它\n")
    out = tools["search_recipes"]("凉面")
    lines = [line for line in out.splitlines() if line.startswith("-")]
    assert any("凉面" in line and "名字" in line for line in lines)
    assert any("红烧肉" in line and "内容" in line for line in lines)


def test_search_returns_a_fragment_not_the_whole_dish(tools):
    long_recipe = "# 长做法\n" + "前面的话。\n" * 200 + "关键句:少放酱油\n"
    tools["write_recipe"]("长做法", long_recipe)
    out = tools["search_recipes"]("关键句")
    assert "关键句" in out
    assert len(out) < len(long_recipe) / 2


def test_search_says_something_human_when_nothing_matches(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    out = tools["search_recipes"]("佛跳墙")
    assert "list_recipes" in out


def test_search_refuses_an_empty_query(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    out = tools["search_recipes"]("   ")
    assert "search_recipes" in out  # 说了该怎么调
    assert not [line for line in out.splitlines() if line.startswith("- ")]  # 没顺手列全部


def test_search_pages_and_reports_the_total(tools):
    for i in range(25):
        tools["write_recipe"](f"菜{i:02d}", "都写了酱油\n")
    first = tools["search_recipes"]("酱油", page=1)
    assert "25" in first
    second = tools["search_recipes"]("酱油", page=2)
    assert first != second
    assert tools["search_recipes"]("酱油", page=999) == tools["search_recipes"]("酱油", page=3)


def test_search_never_reads_the_trash(tools):
    tools["write_recipe"]("红烧肉", "冰糖一块\n")
    tools["delete_recipe"]("红烧肉", "不要了")
    assert "红烧肉" not in tools["search_recipes"]("冰糖")


def test_search_sees_an_edit_made_behind_its_back(tools, root):
    """★ 不建索引的意义:用户用编辑器直接改了那份 `.md`,搜索立刻看得见。

    有索引的话这一刻就变味了,**而且变味之后没有任何报错**——搜不到的东西和
    "确实没写过"长得一模一样。
    """
    tools["write_recipe"]("番茄炒鸡蛋", "# 番茄炒鸡蛋\n")
    (root / "番茄炒鸡蛋.md").write_text("# 番茄炒鸡蛋\n用意大利香草\n", encoding="utf-8")
    assert "番茄炒鸡蛋" in tools["search_recipes"]("意大利香草")


# ── rename:打错了得能救 ───────────────────────────────────────────────


def test_rename_moves_the_dish_without_touching_a_byte(tools):
    tools["write_recipe"]("番茄炒鸡旦", RECIPE)
    out = tools["rename_recipe"]("番茄炒鸡旦", "番茄炒鸡蛋")
    assert tools["read_recipe"]("番茄炒鸡蛋") == RECIPE  # 正文逐字节还是那份
    assert "没有" in tools["read_recipe"]("番茄炒鸡旦")  # 老名字下面空了
    assert "番茄炒鸡蛋" in out


def test_rename_refuses_when_the_new_name_already_exists(tools):
    """合并两份做法是静默的破坏——拒绝,让用户自己看一眼。"""
    tools["write_recipe"]("番茄炒鸡旦", "旦版\n")
    tools["write_recipe"]("番茄炒鸡蛋", "蛋版\n")
    out = tools["rename_recipe"]("番茄炒鸡旦", "番茄炒鸡蛋")
    assert tools["read_recipe"]("番茄炒鸡旦") == "旦版\n"
    assert tools["read_recipe"]("番茄炒鸡蛋") == "蛋版\n"
    assert "已经有" in out


def test_rename_to_the_same_name_says_so_and_does_nothing(tools):
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    out = tools["rename_recipe"]("番茄炒鸡蛋", " 番茄炒鸡蛋 ")
    assert tools["read_recipe"]("番茄炒鸡蛋") == RECIPE
    assert "一样" in out or "没变" in out


# ── delete:移到 .trash,不是 unlink ───────────────────────────────────


def test_delete_moves_the_file_into_the_trash(tools, root):
    """★ M5-20:移走不是删掉。文件必须还在盘上,字节一个不少。"""
    tools["write_recipe"]("红烧肉", RECIPE)
    tools["delete_recipe"]("红烧肉", "做不好吃")
    assert not (root / "红烧肉.md").exists()
    trashed = root / st.TRASH_DIR / "红烧肉.md"
    assert trashed.exists()
    assert trashed.read_text(encoding="utf-8") == RECIPE


def test_undo_brings_it_back_byte_for_byte(tools):
    tools["write_recipe"]("红烧肉", RECIPE)
    tools["delete_recipe"]("红烧肉", "做不好吃")
    out = tools["delete_recipe"]("红烧肉", "删错了", undo=True)
    assert tools["read_recipe"]("红烧肉") == RECIPE
    assert "红烧肉" in out


def test_delete_needs_a_reason(tools):
    tools["write_recipe"]("红烧肉", RECIPE)
    out = tools["delete_recipe"]("红烧肉", "   ")
    assert tools["read_recipe"]("红烧肉") == RECIPE
    assert "为什么" in out


def test_deleting_twice_says_it_is_already_gone(tools):
    tools["write_recipe"]("红烧肉", RECIPE)
    tools["delete_recipe"]("红烧肉", "第一次")
    out = tools["delete_recipe"]("红烧肉", "第二次")
    assert "已经" in out
    assert "第一次" in tools["list_recipes"](include_deleted=True), "第二次的理由盖掉了第一次的"


def test_undo_when_nothing_was_deleted_says_so(tools):
    tools["write_recipe"]("红烧肉", RECIPE)
    out = tools["delete_recipe"]("红烧肉", "拿回来", undo=True)
    assert tools["read_recipe"]("红烧肉") == RECIPE
    assert "没删" in out or "没有删" in out


def test_deleting_a_name_that_was_deleted_before_refuses_instead_of_clobbering(tools):
    """回收站里已经躺着一份同名的:覆盖它就是**把上一次删掉的那份销毁**。"""
    tools["write_recipe"]("红烧肉", "第一版\n")
    tools["delete_recipe"]("红烧肉", "第一次删")
    tools["write_recipe"]("红烧肉", "第二版\n")
    out = tools["delete_recipe"]("红烧肉", "第二次删")
    assert tools["read_recipe"]("红烧肉") == "第二版\n"
    assert "回收站" in out or "已经有" in out


def test_undo_refuses_when_a_live_dish_holds_the_name(tools):
    tools["write_recipe"]("红烧肉", "第一版\n")
    tools["delete_recipe"]("红烧肉", "删了")
    tools["write_recipe"]("红烧肉", "第二版\n")
    out = tools["delete_recipe"]("红烧肉", "拿回来", undo=True)
    assert tools["read_recipe"]("红烧肉") == "第二版\n"
    assert "已经有" in out or "占着" in out


# ── 换行:这个 bundle 的正文全是换行撑起来的 ────────────────────────────


def test_the_returned_recipe_keeps_its_line_breaks(tools):
    """★ M6-5 探针量到的事实:工具结果**在调用它的那一轮原样进模型**,
    组装器的折行与 200 字截断只作用在历史轮。所以这里唯一正确的事是**别动它**
    ——把换行换成 `① ②` 之类反而会毁掉"逐字节一致"那条硬口径。论证见 REVIEW。
    """
    tools["write_recipe"]("番茄炒鸡蛋", RECIPE)
    got = tools["read_recipe"]("番茄炒鸡蛋")
    assert got.count("\n") == RECIPE.count("\n")
    assert "1. 烫番茄 | 去皮" in got.splitlines()


def test_lines_that_are_one_per_row_fold_the_text_they_embed(tools):
    """反过来:**一行一条**的输出里,嵌进去的文本必须折行。

    不折的话,一条删除理由就能凭换行伪造出后续列表项,而伪造出来的那行和真条目
    形式上一模一样(P1-2)。而当轮没有任何上游会替我们折——探针量过。
    """
    tools["write_recipe"]("红烧肉", RECIPE)
    tools["delete_recipe"]("红烧肉", "不要了\n- 番茄炒鸡蛋(已删:伪造的)")
    out = tools["list_recipes"](include_deleted=True)
    assert len([line for line in out.splitlines() if line.startswith("- ")]) == 1
