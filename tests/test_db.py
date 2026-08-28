"""db 层事务/连接的工具级测试。"""

import pytest

from lararium.db import connect, transaction


def test_transaction_rolls_back_on_base_exception(tmp_path):
    """Ctrl-C(KeyboardInterrupt/SystemExit = BaseException)落在事务中间也要 ROLLBACK:
    否则 conn.in_transaction 仍是 True,之后写库全报 cannot start a transaction。"""
    conn = connect(tmp_path / "t.sqlite")
    with pytest.raises(KeyboardInterrupt), transaction(conn):
        conn.execute("INSERT INTO notice_log (date) VALUES ('2026-08-20')")
        raise KeyboardInterrupt()
    assert conn.in_transaction is False, "BaseException 也必须回滚,连接要干净"
    # 回滚后连接可用,事务照常(被回滚的行不在)
    with transaction(conn):
        conn.execute("INSERT INTO notice_log (date) VALUES ('2026-08-21')")
    assert conn.execute("SELECT COUNT(*) FROM notice_log").fetchone()[0] == 1


def test_transaction_is_reentrant_and_the_inner_failure_rolls_back_everything(tmp_path):
    """嵌套的 `transaction` 并入外层,内层失败 → **外层一起回滚**。

    加这一支是因为 M4-7 撞上了:`Journal.append` 自带事务,于是"起居注两条 + 出件箱一条
    一起成或一起不成"根本拼不出来,只能留下半条。不可重入等于**禁止任何两个写库操作
    组合成一个原子动作**——那是限制不是保护。
    """
    conn = connect(tmp_path / "t.sqlite")
    conn.execute("CREATE TABLE t (v TEXT)")

    with pytest.raises(RuntimeError), transaction(conn):
        conn.execute("INSERT INTO t VALUES ('外层')")
        with transaction(conn):  # 并入外层,不再 BEGIN
            conn.execute("INSERT INTO t VALUES ('内层')")
        raise RuntimeError("外层后来炸了")

    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0, "内层没跟着外层回滚"
    assert not conn.in_transaction, "事务没收干净,之后写库会全报 cannot start a transaction"


def test_nested_transaction_commits_once_with_the_outer(tmp_path):
    """正常路径:嵌套不重复提交,外层出块时一次性落盘。"""
    conn = connect(tmp_path / "t.sqlite")
    conn.execute("CREATE TABLE t (v TEXT)")

    with transaction(conn):
        with transaction(conn):
            conn.execute("INSERT INTO t VALUES ('内层')")
        assert conn.in_transaction, "内层出块就提交了,外层的原子性没了"

    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1


def _nest(conn, *, swallow: bool):
    """外层事务里套一个失败的内层;swallow=True 时中间层把异常吞掉、外层继续走。"""
    with transaction(conn):
        conn.execute("INSERT INTO t VALUES ('outer-before')")
        try:
            with transaction(conn):
                conn.execute("INSERT INTO t VALUES ('inner-半条')")
                raise RuntimeError("内层炸了")
        except RuntimeError:
            if not swallow:
                raise
        conn.execute("INSERT INTO t VALUES ('outer-after')")


def test_a_swallowed_inner_failure_still_rolls_back_only_the_inner_block(tmp_path):
    """★ 中间层**吞掉**内层异常时,内层要单独回滚,外层照常提交。

    可重入的第一版是"并入外层、什么都不做",于是这条不成立:没人中途 catch 时看着对
    (异常一路冒到外层,整体回滚),一旦有人吞掉,`inner-半条` 就跟着外层一起提交了
    ——而这次改动修的恰恰是"不许留下半条",换个姿势又能留下半条。

    SAVEPOINT 让"内层失败只回滚内层"**无条件成立**,不用再附加"只要没人 catch"。
    """
    conn = connect(tmp_path / "t.sqlite")
    conn.execute("CREATE TABLE t (v TEXT)")

    _nest(conn, swallow=True)

    assert [r[0] for r in conn.execute("SELECT v FROM t")] == ["outer-before", "outer-after"]


def test_an_unswallowed_inner_failure_rolls_back_everything(tmp_path):
    """没人中途 catch 时仍是整体回滚——SAVEPOINT 不能把这一条弄丢。"""
    conn = connect(tmp_path / "t.sqlite")
    conn.execute("CREATE TABLE t (v TEXT)")

    with pytest.raises(RuntimeError):
        _nest(conn, swallow=False)

    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    assert not conn.in_transaction


def test_a_swallowed_base_exception_also_rolls_back_the_inner_block(tmp_path):
    """`BaseException` 一并带过去:Ctrl-C 落在内层中间也得回滚(R3-1 修过的那条)。"""
    conn = connect(tmp_path / "t.sqlite")
    conn.execute("CREATE TABLE t (v TEXT)")

    with transaction(conn):
        try:
            with transaction(conn):
                conn.execute("INSERT INTO t VALUES ('inner-半条')")
                raise KeyboardInterrupt
        except KeyboardInterrupt:
            pass
        conn.execute("INSERT INTO t VALUES ('outer')")

    assert [r[0] for r in conn.execute("SELECT v FROM t")] == ["outer"]
