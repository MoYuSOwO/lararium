"""M5-31 finance:并发下的「读了再改」。

## bug 是什么

`amend_expense` 先 `SELECT ... fetchone()` 读整行,再 `UPDATE` 把**四个字段一起写**,
而写进去的值全部来自那次读——**读和写之间没有事务**。M5-8 已经确认过:一条 assistant
消息里的多个 `tool_call` 是**并发执行**的(框架把同步工具丢线程池)。于是两个 amend
打同一行:

    线程 A  读到 (49元, 餐饮, 无备注)   要改金额 → 写 (22元, 餐饮, 无备注)
    线程 B  读到 (49元, 餐饮, 无备注)   要改备注 → 写 (49元, 餐饮, 「咖啡」)
                                          ↑ 把 A 改好的金额盖回 49 了
    两边都报成功。

`GuardedConnection` 那把锁挡不住这个——它保的是「一条语句/一个事务内部不被打断」,
而这里是**两条独立语句之间**的窗口。`delete_expense` 是同一个形状:判过 `deleted_at`
才动手,判和动手之间一样敞着。

## 为什么不照着「丢更新」的概率写

M5-8 的教训原话:复现是概率性的,照着崩溃写就是一条**靠运气变绿**的测试。所以这里
钉的是机制,窗口由探针撑到最大,而不是指望调度恰好踩中:

- `LockProbe` 换掉连接的那把锁,**在真正的临界区上**撑窗口、数人头;
- 第一个进临界区的线程在里面等到另一个线程**排上队**才走 → 重叠是必然,不是运气;
- 一个线程**第二次**进顶层临界区(= 它的读和写压根不在同一个临界区里)时,等到
  另一个线程**也读完了**才走 → 丢更新是必然,不是运气。修好之后一次 amend 只有一个
  临界区,这一支根本不会触发,也就不会有人被等死;
- `contended >= 1` 是阳性对照:一个线程**非重入地**拿不到锁,只可能是另一个线程正
  持着它。少了它,断言全绿可能只是因为压根没并发过(T6 第三种假绿)。
"""

import sqlite3
import threading
from pathlib import Path

import pytest
from bundles.finance import server
from bundles.finance.server import build

SHANGHAI = "Asia/Shanghai"

# 所有等待的上限。写错了要以断言红,不许挂死整轮门禁。
WAIT = 5.0


class LockProbe:
    """替换 `GuardedConnection.lock` 的探针:撑开并发窗口,并数出真实的争抢。

    **不是 mock 自己的代码**(T2):底下那把锁是生产的那一把,探针只是包在外面。
    产品里的锁一旦删掉,探针什么都保不住——和 M5-8 重写 `_guarded` 是同一种做法。

    `contended` 只在**非重入地拿不到锁**时才加:同一线程重入 `RLock` 永远立刻成功,
    所以这个数里没有「自己撞自己」的假象,它就是「另一个线程当时正持着这条连接」。
    """

    def __init__(self, inner: threading.RLock) -> None:
        self._inner = inner
        self._cv = threading.Condition()
        self._depth: dict[int, int] = {}  # 每线程的重入深度(只有自己写,GIL 下够用)
        self._left: set[int] = set()  # 走完过至少一个顶层临界区的线程
        self.contended = 0
        self.entered = threading.Event()  # 有线程进到顶层临界区里面了
        self._hold_first = False

    def arm(self) -> None:
        """装置就绪:从这里开始撑窗口、数人头(前面铺数据那几笔不算)。"""
        with self._cv:
            self._depth.clear()
            self._left.clear()
            self.contended = 0
            self.entered.clear()
            self._hold_first = True

    def __enter__(self) -> "LockProbe":
        me = threading.get_ident()
        top = self._depth.get(me, 0) == 0
        if top and me in self._left:
            # 第二次进顶层临界区 = 这个线程的读和写**不在同一个临界区里**,那个窗口
            # 是真实存在的。把它撑到最大:等另一个线程也读完了再往下走。
            self._wait(lambda: bool(self._left - {me}))
        got = self._inner.acquire(blocking=False) if top else False
        if top and not got:
            with self._cv:
                self.contended += 1
                self._cv.notify_all()
        if not got:
            self._inner.acquire()
        self._depth[me] = self._depth.get(me, 0) + 1
        if top:
            self.entered.set()
            if self._hold_first:
                self._hold_first = False
                # 在临界区**里面**等另一个线程排上队:重叠因此是编排出来的,不是撞上的
                self._wait(lambda: self.contended >= 1)
        return self

    def __exit__(self, *exc: object) -> bool:
        me = threading.get_ident()
        self._depth[me] -= 1
        if self._depth[me] == 0:
            with self._cv:
                self._left.add(me)
                self._cv.notify_all()
        self._inner.release()
        return False

    def _wait(self, ready) -> None:
        with self._cv:
            self._cv.wait_for(ready, timeout=WAIT)


@pytest.fixture
def probed(tmp_path, monkeypatch):
    """生产的 bundle、生产的连接,只把那把锁换成探针。

    连接是 `build()` 在里面建的,拿不到手;所以在 `open_connection` 上挂一个**直通**
    的间谍——它照常调真函数,只是把返回的那条连接留一份。换的是锁,不是行为。
    """
    real = server.open_connection
    holder: dict[str, object] = {}

    def spy(path):
        conn = real(path)
        holder["conn"] = conn
        return conn

    monkeypatch.setattr(server, "open_connection", spy)
    runtime = build(tmp_path, timezone=SHANGHAI)
    conn = holder["conn"]
    probe = LockProbe(conn.lock)
    conn.lock = probe
    return runtime, probe


@pytest.fixture
def statements(tmp_path, monkeypatch):
    """同上的直通间谍,记下**发出去的每一句 SQL**。给「没有别的可观测面」的那条用。"""
    real = server.open_connection
    said: list[str] = []

    def spy(path):
        conn = real(path)
        passthrough = conn.execute

        def watched(sql, parameters=(), /):
            said.append(sql)
            return passthrough(sql, parameters)

        conn.execute = watched
        return conn

    monkeypatch.setattr(server, "open_connection", spy)
    return build(tmp_path, timezone=SHANGHAI), said


def tool(runtime, name: str):
    return next(f for f in runtime.tools if f.__name__ == name)


def all_rows(data_dir: Path) -> list[sqlite3.Row]:
    """查全部行,含已删的——这个文件关心的是「库里最后到底长什么样」。"""
    conn = sqlite3.connect(data_dir / "finance" / "finance.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM expenses ORDER BY id"))
    finally:
        conn.close()


def seed_one(runtime, tmp_path) -> int:
    """铺一笔 49 元的餐饮,返回它的 #id。

    时间摆在 23:49 是照 M5-25 的规矩:渲染出来带 `23:49`,谁要是拿裸数字 `"49"`
    断金额,当场露馅。
    """
    tool(runtime, "record_expense")(amount=49, category="餐饮", occurred_at="2026-09-08 23:49")
    return all_rows(tmp_path)[0]["id"]


def run_together(*calls) -> list[str]:
    """两件事同时开工。`Barrier` 只保证同时**出发**,真正的重叠由探针编排。"""
    gate = threading.Barrier(len(calls))
    out: dict[int, str] = {}

    def runner(index: int, call) -> None:
        gate.wait(WAIT)
        out[index] = call()

    threads = [threading.Thread(target=runner, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(WAIT * 4)
    assert not any(t.is_alive() for t in threads), "有线程没回来——编排把自己等死了"
    return [out[i] for i in sorted(out)]


def test_two_amends_on_the_same_row_both_survive(probed, tmp_path):
    """★ M5-31 的要害:两个 amend 改**不同字段**,两边的改动都必须活下来。

    读和写不在一个事务里的话,后写的那个会把它没改的字段按**自己读到的旧值**写回去
    ——而那正是另一个刚改好的。两边都报成功,用户看不到任何异常,账上那个数是错的。
    """
    runtime, probe = probed
    amend = tool(runtime, "amend_expense")
    rid = seed_one(runtime, tmp_path)
    probe.arm()

    outs = run_together(
        lambda: amend(expense_id=rid, amount=22),
        lambda: amend(expense_id=rid, note="咖啡"),
    )

    assert probe.contended >= 1, "两个 amend 根本没重叠,这条测试什么都没测到"
    assert all("改了" in out for out in outs), f"两边都得报成功,丢更新才谈得上:{outs}"
    row = all_rows(tmp_path)[0]
    assert (row["amount_cents"], row["note"]) == (2200, "咖啡"), (
        f"有一边的改动被另一边按旧值盖回去了:{dict(row)};回话:{outs}"
    )


def test_a_delete_landing_mid_amend_leaves_a_story_that_can_be_told_in_order(probed, tmp_path):
    """★「判的时候没删、写的时候已删」不许发生。

    两条回话必须拼得出一个**合法的先后**:要么 delete 在前(amend 回「改不了」,金额
    一分没动),要么 amend 在前(那 delete 跟用户报的就得是改完的样子)。两边**各自
    看着对方动手之前的那一份行**,是任何顺序都排不出来的——那正是读改写敞着的形状。

    编排:先放 delete 进临界区,确认它进去了再放 amend。修好之后 delete 整块持锁,
    amend 只能等它提交完再读,于是必然看见「已经删了」。
    """
    runtime, probe = probed
    rid = seed_one(runtime, tmp_path)
    probe.arm()
    outs: dict[str, str] = {}

    def remove() -> None:
        outs["delete"] = tool(runtime, "delete_expense")(expense_id=rid, reason="记重了")

    def change() -> None:
        outs["amend"] = tool(runtime, "amend_expense")(expense_id=rid, amount=22)

    first = threading.Thread(target=remove)
    first.start()
    assert probe.entered.wait(WAIT), "delete 没进临界区,后面的编排全不成立"
    second = threading.Thread(target=change)
    second.start()
    for t in (first, second):
        t.join(WAIT * 4)
    assert not (first.is_alive() or second.is_alive()), "有线程没回来——编排把自己等死了"

    assert probe.contended >= 1, "两个工具根本没重叠,这条测试什么都没测到"
    row = all_rows(tmp_path)[0]
    assert row["deleted_at"] is not None and "删了" in outs["delete"], (
        f"删除本身就没成,这条测试测的不是它要测的东西:{dict(row)};回话:{outs}"
    )
    if "改不了" in outs["amend"]:
        assert row["amount_cents"] == 4900, f"amend 说没改,账上却变了:{dict(row)};回话:{outs}"
    else:
        assert "22.00 元" in outs["delete"], (
            "amend 说改了,delete 报给用户的却还是它改之前那一份——"
            f"两边各看各的旧行,这个顺序排不出来:{outs}"
        )


def test_reading_before_writing_takes_the_write_lock_up_front(statements, tmp_path):
    """改和删都是「读了再改」,一上来就拿写锁(`BEGIN IMMEDIATE`)。

    **这条没有别的可观测面**:同进程里 `GuardedConnection` 那把可重入锁已经把并发串好
    了,`IMMEDIATE` 防的是独立容器形态(`create_server`)下的**另一条连接**——先读快照
    再写会撞 BUSY_SNAPSHOT,那时候用户收到的是一句"库写入失败"。所以钉的是发出去的
    那句 SQL,和 M5-8 `test_claiming_takes_the_write_lock_up_front` 同一种 T1 例外。
    """
    runtime, said = statements
    rid = seed_one(runtime, tmp_path)

    said.clear()
    tool(runtime, "amend_expense")(expense_id=rid, amount=22)
    assert said[0] == "BEGIN IMMEDIATE", f"amend 的读-改-写没有一上来就拿写锁:{said}"

    said.clear()
    tool(runtime, "delete_expense")(expense_id=rid, reason="记重了")
    assert said[0] == "BEGIN IMMEDIATE", f"delete 的读-改-写没有一上来就拿写锁:{said}"


def test_recording_a_refund_reads_and_writes_inside_one_transaction(statements, tmp_path):
    """M6-3:退款的 `of_expense_id` 校验也是「读了再改」,同一条规矩。

    读到那笔支出还在账上、判过、然后插入——判和插之间敞着的话,一次 `delete_expense`
    正好落在中间,库里就留下一条**指向一条已经不在账上的支出**的退款,而回话说得像
    办成了:用户以为这个月少花了 600,账上并没有。

    **这条为什么钉 SQL 序列,而不是拿探针编排一次并发**:我先写的是后者,而它
    **修好之前和修好之后都绿**——两种顺序的最终库态一模一样(退款行在、它指的那笔
    支出已删),差别只在"插入的那一刻那行还活着吗",事后没有任何可观测面。一条分不出
    bug 和修复的测试正是 T6 第 5 种(断言锚点太弱),按 G6 就不该存在。所以钉的是
    发出去的那几句:`BEGIN IMMEDIATE` 打头(理由同上面那条,给独立容器形态的另一条
    连接),而 SELECT 和 INSERT 都夹在 BEGIN 和 COMMIT 之间——把读挪出事务,这里立刻红。
    """
    runtime, said = statements
    rid = seed_one(runtime, tmp_path)

    said.clear()
    tool(runtime, "record_income")(
        amount=600, kind="refund", occurred_at="2026-09-12 10:00", of_expense_id=rid
    )

    assert said[0] == "BEGIN IMMEDIATE", f"退款的读-改-写没有一上来就拿写锁:{said}"
    assert [s.split()[0] for s in said] == ["BEGIN", "SELECT", "INSERT", "COMMIT"], (
        f"读和写不在同一个事务里:{said}"
    )


def test_two_deletes_on_the_same_row_do_not_both_report_success(probed, tmp_path):
    """delete 也是「读了再改」:两个并发的删除里只有一个能报「删了」。

    都报成功的话,库里留下的理由是后写的那一个,而**先写的那个用户也收到了确认**
    ——「再删一次不该覆盖第一次的理由」那条注释在并发下就是空的。
    """
    runtime, probe = probed
    remove = tool(runtime, "delete_expense")
    rid = seed_one(runtime, tmp_path)
    probe.arm()

    outs = run_together(
        lambda: remove(expense_id=rid, reason="记重了"),
        lambda: remove(expense_id=rid, reason="其实没花"),
    )

    assert probe.contended >= 1, "两个 delete 根本没重叠,这条测试什么都没测到"
    landed = [out for out in outs if "删了" in out]
    assert len(landed) == 1, f"只能有一个报「删了」,另一个得看见「已经删过了」:{outs}"
    row = all_rows(tmp_path)[0]
    assert row["deleted_reason"] in ("记重了", "其实没花")
    assert row["deleted_reason"] in landed[0], (
        f"账上留的理由不属于那个报成功的人:{dict(row)};回话:{outs}"
    )
