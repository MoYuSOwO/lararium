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
