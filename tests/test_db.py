"""db 层事务/连接的工具级测试。"""

import sqlite3
import threading
import time

import pytest

from lararium.db import SCHEMA, GuardedConnection, connect, transaction
from lararium.steward.inbox import Inbox


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


def test_an_old_database_gets_the_new_column(tmp_path):
    """老库要能补上新列。

    `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作——新字段只写进 SCHEMA 的话,
    **新装的机器好使,你自己那台不好使**,而且症状出现在运行时(写入报 no such column),
    不在启动时。这是最难查的那一类,所以补列要机械化、启动时就做。
    """
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE inbox (id TEXT PRIMARY KEY, source TEXT NOT NULL, channel TEXT NOT NULL,"
        " content TEXT NOT NULL, meta TEXT NOT NULL, ts TEXT NOT NULL,"
        " state TEXT NOT NULL DEFAULT 'pending', error TEXT, claimed_at TEXT,"
        " completed_at TEXT, attempts INTEGER NOT NULL DEFAULT 0)"
    )
    old.execute(
        "INSERT INTO inbox (id, source, channel, content, meta, ts)"
        " VALUES ('a', 'user', 'cli', '老消息', '{}', '2026-01-01T00:00:00+00:00')"
    )
    old.commit()
    old.close()

    conn = connect(path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(inbox)")}
    assert "attachments" in columns
    # 老行也要能读回来:补列带了默认值,不是留一堆 NULL 等着在 json.loads 里炸
    assert conn.execute("SELECT attachments FROM inbox WHERE id='a'").fetchone()[0] == "[]"
    assert connect(path) is not None, "补列要幂等,第二次启动不许报 duplicate column"


# ── M5-8:一条连接被并发使用 ─────────────────────────────────────────────


class Probe(GuardedConnection):
    """在**真正的临界区之内**数人头。

    重写的是 `_guarded` 而不是 `execute`:探针必须落在锁**里面**。落在外面的话,
    两个线程会同时"在里面"——其中一个只是在等锁——那是假阳性;而如果探针自己
    再上一把锁,产品里的锁被删掉它也照样绿,那是假阴性(自己给自己站岗)。
    """

    inside = 0
    max_inside = 0
    queued = 0
    max_queued = 0

    def execute(self, sql, parameters=(), /):
        self.statements = [*getattr(self, "statements", []), sql]
        return super().execute(sql, parameters)

    def _guarded(self, call):
        self.queued += 1
        self.max_queued = max(self.max_queued, self.queued)
        try:
            return super()._guarded(self._watch(call))
        finally:
            self.queued -= 1

    def _watch(self, call):
        def inner():
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)
            time.sleep(0.0005)  # 把窗口撑开:没有锁的话必然重叠
            try:
                return call()
            finally:
                self.inside -= 1

        return inner


def probe_connection(path):
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, factory=Probe)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def test_no_two_threads_are_ever_inside_the_connection(tmp_path):
    """★ M5-8 的机制:**任一时刻只有一个线程在这条连接里面。**

    这不是"崩不崩"的问题。`check_same_thread=False` 关掉的只是那个守卫,不是让连接
    变线程安全——两个线程交错 execute/fetch,pysqlite 的游标与语句缓存就烂了,
    表现是三种毫不相干的面孔(IndexError / InterfaceError / TypeError),概率性出现。
    照着崩溃写测试就是一条靠运气变绿的测试,比没有还糟,所以这里钉的是机制。

    `max_queued >= 2` 是阳性对照:证明真的有多个线程在抢这条连接。少了它,
    `max_inside == 1` 可能只是因为压根没并发过(T6 第三种假绿)。
    """
    conn = probe_connection(tmp_path / "probe.sqlite")
    for i in range(40):
        conn.execute(
            "INSERT INTO journal (envelope_id, kind, payload, ts) VALUES (?,?,?,?)",
            (f"env-{i}", "envelope", "{}", "2026-08-30T00:00:00+00:00"),
        )

    workers = 4
    gate = threading.Barrier(workers)

    def hammer():
        gate.wait()  # 让四个线程**确实**同时开工,不靠调度运气
        for _ in range(20):
            conn.execute("SELECT COUNT(*) FROM journal").fetchone()
            conn.execute("SELECT * FROM journal ORDER BY seq LIMIT 5").fetchall()

    threads = [threading.Thread(target=hammer) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert conn.max_queued >= 2, (
        f"根本没并发起来,这条测试什么都没测到(max_queued={conn.max_queued})"
    )
    assert conn.max_inside == 1, f"有 {conn.max_inside} 个线程同时在连接里面"


def test_a_transaction_holds_the_connection_for_the_whole_block(tmp_path):
    """粒度是**一个事务**,不是一条语句。

    只锁单条语句的话,另一个线程能挤进 BEGIN 和 COMMIT 之间:它的写入会掉进别人的
    事务里(崩在中间就是"回复落了、信封没标完成"→ 重启后重复回复),而且它自己那句
    `BEGIN` 会直接报 cannot start a transaction within a transaction。
    """
    conn = connect(tmp_path / "t.sqlite")
    conn.execute("CREATE TABLE t (v TEXT)")
    opened = threading.Event()
    order: list[str] = []

    def outsider():
        opened.wait(5)
        conn.execute("INSERT INTO t VALUES ('outsider')")
        order.append("outsider")

    t = threading.Thread(target=outsider)
    t.start()
    with transaction(conn):
        conn.execute("INSERT INTO t VALUES ('inside')")
        opened.set()
        time.sleep(0.05)  # 给另一个线程充足的时间挤进来
        order.append("inside")
    t.join(5)

    assert order == ["inside", "outsider"], f"事务块中间被别的线程挤进来了:{order}"


def test_execute_hands_back_rows_not_a_live_cursor(tmp_path):
    """锁在 `execute` 返回的那一刻就放了,所以**行必须在临界区里面取干净**。

    交出一个活游标等于把洞挪了个位置:两个线程各拿一个游标、交错地 step 同一条连接
    ——`InterfaceError: bad parameter or other API misuse` 正是这么来的。
    这条钉的是"不许把活游标交出去",顺带钉住"别为了不是游标就把结果丢了"。
    """
    conn = connect(tmp_path / "rows.sqlite")
    conn.execute(
        "INSERT INTO journal (envelope_id, kind, payload, ts) VALUES (?,?,?,?)",
        ("env-1", "envelope", "{}", "2026-08-30T00:00:00+00:00"),
    )

    result = conn.execute("SELECT envelope_id FROM journal")

    assert not isinstance(result, sqlite3.Cursor), "把活游标交出去了,锁就盖不住取行那一半"
    assert [r["envelope_id"] for r in result] == ["env-1"]


def test_claiming_takes_the_write_lock_up_front(tmp_path):
    """认领是"读了再改",必须一上来就拿写锁(`BEGIN IMMEDIATE`)。

    连接内部的并发由那把锁挡住了,但**别的进程**(备份脚本、手工开的 sqlite3、
    误起的第二个实例)不在锁的作用域里;普通 `BEGIN` 会在 UPDATE 那一步才尝试升级,
    升不上去就是 SQLITE_BUSY。这条没有别的可观测面,所以钉的是发出去的那句 SQL
    ——和"原子写只能钉 fsync+rename"是同一种例外,写在这里免得下一个人以为是疏忽。
    """
    conn = probe_connection(tmp_path / "claim.sqlite")
    Inbox(conn).claim_next()

    assert "BEGIN IMMEDIATE" in conn.statements, f"认领没拿写锁:{conn.statements}"
