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
