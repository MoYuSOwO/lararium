# 验收记录

程序员执行 [PLAN.md](PLAN.md) 的任务,Claude 负责验收。本文件是双方的交接点与审计留痕。

## 协议

**程序员**:完成一个任务后,在下面对应行填「待验收」,并附上两样东西——
1. 该任务测试命令的实际输出(粘贴,不要复述"都过了");
2. 与计划的任何偏离(改了什么、为什么)。计划写错了很正常,如实说,别硬凑。

**Claude**:验收时逐条核对——
1. 自己重跑一遍门禁与测试,不采信转述;
2. 读实现,核对是否满足任务的 Interfaces 契约与全局约束;
3. 按 [CONVENTIONS.md](CONVENTIONS.md) 过一遍,**违反项按编号引用**(如「违反 F1:`meta` 跨模块传递却是裸 dict」)。编号让意见可讨论——被评审的人可以反驳「F1 说的是跨模块,这个没跨」,这比「我觉得这里不太好」有用得多;
4. 特别检查跨任务不变量(见下);
5. 给结论:**通过** / **打回**(附具体问题)。通过后由程序员在 [CHANGELOG.md](CHANGELOG.md) 追加条目。

规范条文本身也可能错。反复挡住合理做法的规则,在验收记录里提出来改掉,别忍着。

**验收第一步永远是跑门禁**(四关任一不过,直接打回,不必往下看):

```bash
uv run ruff check src bundles tests && uv run ruff format --check src bundles tests && uv run mypy && uv run lint-imports && uv run pytest -q
```

**门禁管不了、必须人工核对的三条硬不变量**(它们跨任务,单任务测试测不出退化):

- **前缀字节稳定**:账本不变时,不同轮次的 `system_prompt` 必须逐字相同。凡触碰 `assembler.py`、`registry.py`、`tools.as_tool_functions()`、`Steward.all_tools()` 的改动都要复查。架构测试只能挡住"组装器读时钟"这一种退化,挡不住别的。
- **账本写入单一路径**:架构测试已挡住"别的模块写文件",但挡不住"在 `ledger.py` 里加了第二个写入函数、绕开 `Gate.settle()`"——这条要人看。
- **可见即入账**:落账的 prompt 必须是模型实收的那一份,不是重新拼的;新增的模型可见内容(未来的压缩摘要、状态卡)必须按原文入账。

**还要核对的两条纪律**:

- 新增的 `# noqa` / `# type: ignore` 是否都带了理由,范围是否最小(不是整文件、整规则)。
- 严格档模块(`pyproject.toml` 里 mypy 的 overrides 列表)有没有被偷偷挪进宽松档。改这个列表等同于改设计,必须在验收记录里说明。

---

## M1 任务验收表

| # | 任务 | 状态 | 验收人结论 | CHANGELOG | 日期 |
|---|---|---|---|---|---|
| 1 | 环境、门禁与配置加载 | **通过** | 全绿;conftest 环境隔离已补(commit 2e5470e) | ☑ | 2026-08-17 |
| 2 | 信封模型与收件箱 | **通过** | 全绿;崩溃恢复已补(commit fcea06e) | ☑ | 2026-08-17 |
| 3 | 起居注与中文检索 | **通过** | 全绿;对抗测试无失败,无补做项 | ☑ | 2026-08-17 |
| 4 | 账本文件与快照表 | **通过** | 全绿;read() 纯化已补(commit e2a1395) | ☑ | 2026-08-17 |
| 5 | 门控状态机 | 待验收 | | | |
| 6 | Memory bundle 的 MCP server | 未开始 | | | |
| 7 | 插件注册表与 read_skill | 未开始 | | | |
| 8 | 内置工具三件 | 未开始 | | | |
| 9 | 上下文组装器 | 未开始 | | | |
| 10 | 模型客户端与缓存指标 | 未开始 | | | |
| 11 | 一轮的编排与 CLI | 未开始 | | | |
| 12 | 端到端验收 | 未开始 | | | |

状态取值:未开始 / 进行中 / 待验收 / **通过** / 打回

---

## 验收详情

<!-- 每个任务一节,按下面的模板填。程序员填「执行记录」,Claude 填「验收结论」。

### Task N:任务名

**执行记录**(程序员填)

测试输出:
```
$ uv run pytest tests/... -v
...
```

与计划的偏离:
- (无 / 具体说明)

**验收结论**(Claude 填)

- 门禁四关:ruff ☐ / mypy ☐ / import-linter ☐ / pytest ☐
- 重跑结果:
- 规范核对(CONVENTIONS.md):违反项按编号列出,无则写「无」
- 契约核对:
- 不变量核对:前缀稳定 ☐ / 账本单写 ☐ / 可见即入账 ☐
- 抑制与分档:noqa/ignore 有理由 ☐ / 严格档列表未被削弱 ☐
- 结论:通过 / 打回(原因)
- 通过后:CHANGELOG.md 已追加条目 ☐(程序员补勾)

-->

### Task 1:环境、门禁与配置加载

**执行记录**(程序员填)

测试输出:
```
$ uv run pytest tests/test_config.py -v
============================= test session starts ==============================
platform darwin -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0 -- /Users/jayhuang/Code/lararium/.venv/bin/python
cachedir: .pytest_cache
rootdir: /Users/jayhuang/Code/lararium
configfile: pyproject.toml
plugins: logfire-4.40.0, asyncio-1.4.0, anyio-4.14.2
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 2 items

tests/test_config.py::test_load_reads_env PASSED                         [ 50%]
tests/test_config.py::test_load_rejects_missing_api_key PASSED           [100%]

============================== 2 passed in 0.01s ===============================
```

与计划的偏离:
- Step 1 的 `pre-commit autoupdate` 将 pre-commit-hooks 从 v5.0.0 更新到 v6.0.0,`.pre-commit-config.yaml` 的 `rev` 字段相应改变。这是计划 Step 1 明确要求的步骤(配置注释也写了"装好后跑一次 autoupdate 拉到最新并提交"),一并纳入本次 commit。
- `tests/test_config.py` 的 import 排序:ruff 的 isort(I001)要求 `import pytest` 与 `from lararium.config import Settings` 之间加空行(第三方与第一方分组),计划原文两者紧邻。已按 ruff 要求修正。
- `tests/test_config.py` 的注释对齐:计划原文用多余空格对齐 `# 默认值` 注释,ruff format 会去掉对齐空格。已按 ruff format 要求修正为单空格。
- 环境说明:本机 `uv` 在 `/opt/homebrew/bin` 但不在默认 PATH,且沙箱禁止写 `~/.cache`,故运行 uv 命令时需 `export PATH=/opt/homebrew/bin:$PATH` 与 `UV_CACHE_DIR=<workspace>/.uv-cache`、`PRE_COMMIT_HOME=<workspace>/.pre-commit-cache`。这些是环境适配,不影响代码与门禁结果;`.uv-cache` 由 uv 自带 `.gitignore` 忽略,`.pre-commit-cache` 需确认不进 git(见下)。

**验收结论**(Claude 填)

- 门禁四关:ruff ☑ / mypy ☑(6 files)/ import-linter ☑(3 kept, 0 broken)/ pytest ☑(5 passed, 1 skipped)
- 重跑结果:独立重跑全绿,与报告一致。`tests/test_config.py` 2 passed。
- 规范核对(CONVENTIONS.md):**无违反**。`Settings` 是 frozen dataclass、无模块级可变状态(F5);
  `load()` 只读环境不产生副作用(F4);错误消息带出路「请参考 .env.example」(E3)。
- 契约核对:`Settings` 六个字段与 `load() -> Settings` 与计划 Interfaces 完全一致;
  严格档要求的全量注解齐备(`lararium.config` 在 mypy strict 列表内且通过)。
- 不变量核对:本任务不涉及前缀/账本/起居注,三条均不适用。
- 抑制与分档:无新增 noqa / type: ignore;`pyproject.toml` 未改动,严格档列表完好。
- 卫生检查:`git ls-files` 确认无缓存目录、无 `.env` 被跟踪;`.gitignore` 补的
  `.pre-commit-cache/`、`.uv-cache/` 两行正确(程序员自行发现并补上,这一处做得好)。

**三条偏离全部接受**:

1. `pre-commit autoupdate` 拉到 v6.0.0 —— 这本来就是 Step 1 要求的动作,配置注释也写了。
2、3. ruff 的 import 分组与注释对齐修正 —— **计划原文错了,ruff 是对的**。计划里的代码是我手写的,
没过 formatter;凡此类冲突一律以工具为准,不必回报得这么详细(但报了不扣分)。

**一处必须修的缺陷(我的计划错,不是你的实现错)**:

`tests/test_config.py` 没有和宿主环境隔离。`test_load_reads_env` 只 setenv 了四个变量,
`LARARIUM_TIMEZONE` 和 `LARARIUM_L0_MAX_TURNS` 靠默认值断言——宿主 shell 里一旦有这两个变量,
测试就读到真实配置而失败。实测复现:

```
$ LARARIUM_L0_MAX_TURNS=50 LARARIUM_TIMEZONE=Asia/Tokyo uv run pytest tests/test_config.py -q
E         - Asia/Shanghai
E         + Asia/Tokyo
FAILED tests/test_config.py::test_load_reads_env
```

这个坑必踩:Task 12 Step 4 的冒烟原文就是 `set -a && source .env && set +a`,
之后在同一个 shell 跑全量测试正是 M1 的完成标准。而报错现象和真正原因隔得很远,极难查。

PLAN.md Task 1 已补 **Step 3:写 `tests/conftest.py`**(autouse fixture 清掉所有
`LARARIUM_*`),原 Step 3–8 顺延。请照新 Step 3 补上这个文件,单独 commit,并入 Task 1。
用 autouse 是为了让后续 Task 11、12 的 fixture 自动受益,不必每个任务各自记得。

**结论:通过**(补完 conftest.py 即可开始 Task 2,不必等二次验收)
- 通过后:CHANGELOG.md 已追加条目 ☑

**补完记录**(程序员填,commit 2e5470e)

按新 Step 3 写 `tests/conftest.py`(autouse fixture 清所有 `LARARIUM_*`)。验证隔离生效——
宿主 shell 注入干扰值后测试仍全绿:

```
$ LARARIUM_API_KEY=sk-LEAKED-FROM-HOST LARARIUM_TIMEZONE=Asia/Tokyo \
  LARARIUM_L0_MAX_TURNS=999 uv run pytest tests/test_config.py -v
tests/test_config.py::test_load_reads_env PASSED                         [ 50%]
tests/test_config.py::test_load_rejects_missing_api_key PASSED           [100%]
============================== 2 passed in 0.01s ===============================
```

门禁四关全绿(9 files formatted / mypy 6 files / 3 kept 0 broken / 5 passed 1 skipped)。
单独 commit:`test: 隔离宿主环境变量,堵住 source .env 后测试串读的坑`。CHANGELOG M1 条目已追加。

---

### Task 2:信封模型与收件箱

**执行记录**(程序员填)

测试输出(Step 2 确认失败):
```
$ uv run pytest tests/steward/test_inbox.py -v
...
tests/steward/test_inbox.py:5: in <module>
    from lararium.db import connect
E   ModuleNotFoundError: No module named 'lararium.db'
ERROR tests/steward/test_inbox.py
=============================== 1 error in 0.07s ===============================
```

测试输出(Step 6 确认通过):
```
$ uv run pytest tests/steward/test_inbox.py -v
...
tests/steward/test_inbox.py::test_put_then_claim_returns_same_envelope PASSED [ 16%]
tests/steward/test_inbox.py::test_claim_is_strictly_serial PASSED        [ 33%]
tests/steward/test_inbox.py::test_claim_order_is_oldest_first PASSED     [ 50%]
tests/steward/test_inbox.py::test_claim_returns_none_when_empty PASSED   [ 66%]
tests/steward/test_inbox.py::test_fail_marks_envelope_and_unblocks_queue PASSED [ 83%]
tests/steward/test_inbox.py::test_meta_roundtrips_as_json PASSED         [100%]
============================== 6 passed in 0.07s ===============================
```

与计划的偏离:
- **`src/lararium/db.py` 索引定义 bug(计划代码错)**:计划原文 `CREATE INDEX ... ON inbox(state, ts, rowid)`
  在 SQLite 里报 `no such column: rowid`——隐式 `rowid` 是 b-tree 主键,不能被单独建索引。
  实测确认:`CREATE INDEX ON t(rowid)` 失败,但查询里 `ORDER BY ts, rowid` 正常(rowid 在 rowid 表里
  永远可引用,且随插入单调递增)。修复:索引改为 `ON inbox(state, ts)`;`inbox.py` 查询里的
  `ORDER BY ts, rowid` 保留不动——稳定 tie-break 的意图不变,只是 tie-break 不被索引覆盖,
  个人收件箱全表扫描完全可接受。
- **`src/lararium/steward/inbox.py` `pending_count` 类型(计划代码错)**:`fetchone()[0]` 在 typeshed
  里返回 `Any`(sqlite3.Row.__getitem__ → Any),严格档 `warn_return_any` 拦截了 `-> int` 的返回。
  修复:用 `int(...)` 包一层,最小改动。
- **`datetime.UTC` 替代 `timezone.utc`(工具为准)**:ruff UP017(pyupgrade)要求 py311+ 用 `datetime.UTC`。
  计划原文用 `timezone.utc`,envelope.py / inbox.py / test_inbox.py 三处已按 ruff 自动修为 `UTC`。
  与 Task 1 同类冲突,按既定惯例以工具为准。
- `tests/steward/test_inbox.py` 一处注释对齐空格被 ruff format 去掉(`is None      # ...` → 单空格),
  与 Task 1 同类,已修正。

**验收结论**(Claude 填)

- 门禁四关:ruff ☑ / mypy ☑(9 files)/ import-linter ☑(3 kept, 0 broken)/ pytest ☑(11 passed, 1 skipped)
- 重跑结果:独立重跑全绿。`test_inbox.py` 6 passed,逐条核对了测试名与断言。
- 规范核对(CONVENTIONS.md):**无违反**。`claim_next` 的 `except Exception` 后跟
  ROLLBACK + `raise`,没有吞异常(E1);`_now` / `_conn` 用下划线标出内部性(S3);
  `Envelope` 是有名字的 pydantic 模型而非裸 dict(F1);`Envelope.new` keyword-only(F3);
  无模块级可变状态(F5)。
- 契约核对:`Envelope`(六字段 + `.new` keyword-only)、`connect`、`Inbox` 五个方法
  全部与计划 Interfaces 一致。
- 不变量核对:严格串行是「可重放」的地基,`test_claim_is_strictly_serial` 覆盖到位;
  账本/前缀两条本任务不涉及。
- 抑制与分档:无新增 noqa / type: ignore;`pyproject.toml` 未动。

**四条偏离全部成立,其中两条是计划的真 bug**:

1. **`rowid` 索引** —— 计划写的 `CREATE INDEX ... ON inbox(state, ts, rowid)` 在 SQLite 里非法,
   隐式 rowid 不能被单独索引。改成 `(state, ts)` 正确,而且保留查询里的 `ORDER BY ts, rowid`
   的判断也对:索引的隐式尾列本就是 rowid,tie-break 语义没丢。**分析和取舍都准确。**
2. **`pending_count` 的 `int()` 包装** —— `sqlite3.Row.__getitem__` 在 typeshed 里是 `Any`,
   严格档 `warn_return_any` 拦下来了。这正是分层严格该起的作用,处理方式最小且正确。
3. `datetime.UTC` 替代 `timezone.utc`(ruff UP017)、4. 注释对齐 —— 以工具为准,对。

**一处必须补做的缺陷(我的计划漏了,不是你的实现错)**:

**严格串行 + 持久化状态 + 硬崩溃 = 队列永久卡死。** 我压测复现了:

```
崩溃前认领: 崩之前这条
重启后 pending 数: 1
重启后能认领到: ❌ None —— 队列永久卡死
```

进程被 SIGKILL / 断电 / OOM 杀掉时,那条 `processing` 记录永远留在库里,重启后
`claim_next()` 每次看到 `in_flight=1` 就返回 None。助手从此对所有消息静默——
不报错、不打日志,只是不理你了。这对一个"住在 IM 里随时可用"的系统是致命的,
而且十年运行期内必然发生。

PLAN.md Task 2 已补 **Step 8–13(崩溃恢复)**:加 `attempts` 列、`recover_stale()` 方法、
三个测试;Task 11 的 CLI 启动处也加了调用(否则方法白写)。带重试上限是为了防毒消息——
如果崩溃正是这条消息引起的,无脑重排队会让每次启动都崩一次。

**结论:通过**(补完崩溃恢复即可开始 Task 3,不必等二次验收)
- 通过后:CHANGELOG.md 已追加条目 ☑

**补完记录**(程序员填,commit fcea06e)

按 Step 8–13 补崩溃恢复:

- `db.py` SCHEMA 加 `attempts INTEGER NOT NULL DEFAULT 0`
- `inbox.py` `claim_next()` 的 UPDATE 加 `attempts=attempts+1`;新增 `recover_stale(max_attempts=2) -> (requeued, abandoned)`,
  先标记放弃(已达重试上限的)再重排队剩下的,顺序不能反
- 3 个测试:重启后重排队 / 毒消息放弃 / 干净启动 noop

测试输出(Step 12 确认通过):
```
$ uv run pytest tests/steward/test_inbox.py -v
...
tests/steward/test_inbox.py::test_recover_stale_requeues_interrupted_envelope PASSED [ 77%]
tests/steward/test_inbox.py::test_recover_stale_abandons_poison_message PASSED [ 88%]
tests/steward/test_inbox.py::test_recover_stale_is_noop_on_clean_start PASSED [100%]
============================== 9 passed in 0.06s ===============================
```

门禁四关全绿(14 passed, 1 skipped)。CHANGELOG Task 2 条目已追加。无偏离——计划代码照抄即过。

---

### Task 3:起居注与中文检索

**执行记录**(程序员填)

测试输出(Step 2 确认失败):
```
$ uv run pytest tests/steward/test_journal.py -v
...
tests/steward/test_journal.py:6: in <module>
    from lararium.steward.journal import Journal
E   ModuleNotFoundError: No module named 'lararium.steward.journal'
ERROR tests/steward/test_journal.py
=============================== 1 error in 0.04s ===============================
```

测试输出(Step 5 确认通过):
```
$ uv run pytest tests/steward/test_journal.py -v
...
tests/steward/test_journal.py::test_sqlite_supports_trigram_tokenizer PASSED [ 11%]
tests/steward/test_journal.py::test_append_and_replay_preserves_order_and_content PASSED [ 22%]
tests/steward/test_journal.py::test_replay_is_byte_identical_across_calls PASSED [ 33%]
tests/steward/test_journal.py::test_search_finds_chinese_substring PASSED [ 44%]
tests/steward/test_journal.py::test_search_finds_two_character_word PASSED [ 55%]
tests/steward/test_journal.py::test_search_does_not_index_internal_events PASSED [ 66%]
tests/steward/test_journal.py::test_search_respects_limit PASSED         [ 77%]
tests/steward/test_journal.py::test_search_returns_empty_for_no_match PASSED [ 88%]
tests/steward/test_journal.py::test_recent_turns_returns_newest_last PASSED [100%]
============================== 9 passed in 0.04s ===============================
```

与计划的偏离:
- **`journal.py` `append` 的 `cur.lastrowid` 类型(计划代码错)**:typeshed 里 `sqlite3.Cursor.lastrowid`
  是 `int | None`,`int(cur.lastrowid)` 被 mypy 严格档 `arg-type` 拦截。AUTOINCREMENT 主键的 INSERT
  必有 lastrowid,这是运行时不变量。修复:加 `assert cur.lastrowid is not None` 收窄类型,注释说明理由。
- **`datetime.UTC` 替代 `timezone.utc`(工具为准)**:计划原文用 `timezone.utc`,ruff UP017 要求 `UTC`。
  与 Task 1/2 同类,已按 ruff 修正。
- **`recent_turns` 一行长生成器被 ruff format 拆多行**:纯格式,无逻辑变化。

**验收结论**(Claude 填)

- 门禁四关:ruff ☑ / mypy ☑(10 files)/ import-linter ☑(3 kept, 0 broken)/ pytest ☑(23 passed, 1 skipped)
- 重跑结果:独立重跑全绿。`test_journal.py` 9 passed,含两条中文检索关键测试。
- 规范核对(CONVENTIONS.md):**无违反**。`SearchHit` 是有名字的 frozen dataclass 而非裸元组(F1);
  `_searchable_text` 用下划线标出内部性(S3);`search` 只读、`append` 只写,职责分明(F4);
  `search` 的 docstring 说清了两条路径的分界理由(G1 写为什么)。
- 契约核对:`Journal` 四个方法、`SearchHit` 四个字段、`SEARCHABLE_KINDS` 均与计划一致。
- 不变量核对:**可见即入账**的地基在此。`replay` 按 `seq` 排序且逐字返回落账内容,
  `test_replay_is_byte_identical_across_calls` 守住了幂等性。前缀/账本两条本任务不涉及。
- 抑制与分档:一处 `assert cur.lastrowid is not None` 带理由注释,范围最小,可接受;
  无 noqa / type: ignore;`pyproject.toml` 未动。

**三条偏离全部成立**,`lastrowid` 那条又是计划的真 bug(typeshed 标 `int | None`,
严格档拦得对),用 assert 收窄并注明理由是最小改动。

**额外做了两轮对抗测试,结果都好:**

1. **特殊字符不炸**。模型传进来的查询词不会规矩,而 FTS5 有自己的查询语法。
   实测 14 个刁钻查询(`C++`、`AI/ML`、`「明天见」`、`say "hi"`、`NOT 日料`、`*`、`^abc`、
   `100%`、`a_b`)全部正常返回,无一例 `sqlite3.OperationalError`。
   引号转义与 LIKE 的 `ESCAPE` 都正确;FTS5 的 `NOT`/`OR` 被当字面量处理,
   这对"检索聊天内容"的用途是正确行为。
2. **`recent_turns` 对进行中的轮次行为正确**。当前轮 envelope 已落账但还没 reply 时,
   返回 `assistant=None`,交给 Task 9 的组装器跳过——两边契约对得上:

```
limit=3 : [('问2', '答2'), ('问3', '答3'), ('正在处理的这句', None)]
```

注意由此产生的既定行为:`recent_turns(N)` 在处理途中实际只给出 N-1 轮完整历史。
这是计划里就说明过的,不是缺陷。

**结论:通过**(无补做项,直接开始 Task 4)
- 通过后:CHANGELOG.md 已追加条目 ☑

(待验收)

---

### Task 4:账本文件与快照表

**执行记录**(程序员填)

测试输出(Step 2 确认失败):
```
$ uv run pytest tests/bundles/test_ledger.py -v
...
tests/bundles/test_ledger.py:5: in <module>
    from bundles.memory.ledger import LEDGER_SECTIONS, Ledger, memory_schema
E   ModuleNotFoundError: No module named 'bundles.memory.ledger'
ERROR tests/bundles/test_ledger.py
=============================== 1 error in 0.02s ===============================
```

测试输出(Step 5 确认通过):
```
$ uv run pytest tests/bundles/test_ledger.py -v
...
tests/bundles/test_ledger.py::test_read_creates_file_with_sections PASSED [ 14%]
tests/bundles/test_ledger.py::test_write_persists_and_snapshots PASSED   [ 28%]
tests/bundles/test_ledger.py::test_sync_manual_edit_captures_out_of_band_change PASSED [ 42%]
tests/bundles/test_ledger.py::test_sync_manual_edit_is_noop_when_unchanged PASSED [ 57%]
tests/bundles/test_ledger.py::test_rollback_restores_content_and_records_new_snapshot PASSED [ 71%]
tests/bundles/test_ledger.py::test_history_is_newest_first PASSED        [ 85%]
tests/bundles/test_ledger.py::test_diff_shows_changed_lines PASSED       [100%]
============================== 7 passed in 0.02s ===============================
```

与计划的偏离:
- **Step 3「让 bundles 可导入」已就位,无需改动**:`bundles/__init__.py`、`bundles/memory/__init__.py`
  在 M0 立项时已建,`pyproject.toml` 的 `pythonpath` 已是 `["src", "."]`。故 Step 3 省略,不影响结果。
- **`ledger.py` `snapshot` 的 `cur.lastrowid` 类型(计划代码错,与 Task 3 同类)**:typeshed 标 `int|None`,
  mypy 严格档 `arg-type` 拦截。加 `assert cur.lastrowid is not None` 收窄,注释说明 AUTOINCREMENT 不变量。
- **`datetime.UTC` 替代 `timezone.utc`(工具为准)**:ruff UP017,与前序任务同类。
- **测试 import 排序与未用 import**:计划测试 import 了 `from pathlib import Path` 但未用(ruff F401),
  已删;`import sqlite3` / `import pytest` / `from bundles...` 之间的空行被 ruff isort 去掉。
- 预期失败信息与计划不同:计划写 `No module named 'bundles'`,实际是 `No module named 'bundles.memory.ledger'`
  (bundles 包已存在,缺的只是 ledger 模块)。这是 Step 3 已就位的副作用,不影响 TDD 验证。

**验收结论**(Claude 填)

- 门禁四关:ruff ☑ / mypy ☑(11 files)/ import-linter ☑(3 kept, 0 broken)/ pytest ☑(30 passed, 1 skipped)
- 重跑结果:独立重跑全绿。`test_ledger.py` 7 passed。
- 规范核对(CONVENTIONS.md):`Snapshot` 是 frozen dataclass(F1);`_hash`/`_blank_ledger`
  用下划线标出内部性(S3);`get()` 的 KeyError 带上快照 id(E3);无模块级可变状态(F5)。
  **一处违反,见下(F4/F6)。**
- 契约核对:`Ledger` 八个方法、`Snapshot` 五个字段、`LEDGER_SECTIONS`、`memory_schema()`
  均与计划一致。`rollback` 记为一次新变更(source="rollback"),历史只增不改,正确。
- 不变量核对:**账本单写路径——这次不通过,见下**。前缀/可见即入账两条本任务不涉及。
- 抑制与分档:一处 `assert cur.lastrowid is not None` 带理由,与 Task 3 同类,可接受;
  `pyproject.toml` 未动。

**五条偏离全部成立**,`lastrowid` 与 Task 3 同因,`Path` 未用 import 被 ruff F401 抓出来
说明门禁在正常工作。省略 Step 3 的判断也对——那两个 `__init__.py` 在 M0 就建好了。

**一处必须补做的缺陷(我的设计错,不是你的实现错)**:

这正是 REVIEW 协议里写明「架构测试挡不住、必须人工看」的那一类。`ledger.py` 里有
**两处** `write_text`:`write()` 一处,`read()` 里还有一处。后者在文件不存在时
悄悄建一份空账本——名字是查询,行为在写文件(**违反 F4 命令查询分离、F6 副作用要写进名字**)。

比规范违反更严重的是它的失败模式。组装器每轮都调 `read()`,实测:

```
文件丢失后 read(): '## 身份\n\n## 关系\n\n## 长期偏好\n\n## 正在进行\n'
→ 事实还在吗? ❌ 静默消失了,无报错无日志
→ 历史快照还在吗? 在,1 条(可 rollback 恢复)
```

账本文件因任何原因丢失(误删、M2 的卷没挂上、备份恢复出错),助手就**静默失忆**:
不报错、不打日志,用户只会觉得"它怎么把我说过的全忘了",而且根本查不出原因——
数据其实还在 SQLite 里躺着,但没人会知道要去 rollback。

PLAN.md Task 4 已补 **Step 8–11**:`read()` 改纯读、缺文件抛 `FileNotFoundError` 并
在消息里给出恢复路径;新建职责移交 `ensure_initialized()`,只在启动时调用。
Task 5 的 Gate fixture 与 Task 6 的 `build_memory_components` 已同步改好,做到时照新版写。

改完之后**全代码树只剩 `Ledger.write()` 里一行 `write_text`**——「账本单写路径」
到这时才真正在代码层面成立,而不只是一句口号。

**结论:通过**(补完 `read()` 纯化即可开始 Task 5,不必等二次验收)
- 通过后:CHANGELOG.md 已追加条目 ☑

**补完记录**(程序员填,commit e2a1395)

按 Step 8–11 补 `read()` 纯化:

- `read()` 改纯读,文件缺失抛 `FileNotFoundError`,消息含恢复路径(`history()` + `rollback()`)
- 新增 `ensure_initialized() -> bool`:文件不存在则走 `write()` 建空账本 + init 快照,返回是否新建;存在则返回 False。全代码树只剩 `Ledger.write()` 一处 `write_text`
- 测试:删 `test_read_creates_file_with_sections`,换三个(`ensure_initialized` 建文件 / noop / `read` 缺文件抛错且历史可恢复)

测试输出(Step 10 确认通过):
```
$ uv run pytest tests/bundles/test_ledger.py -v
...
tests/bundles/test_ledger.py::test_ensure_initialized_creates_file_with_sections PASSED [ 11%]
tests/bundles/test_ledger.py::test_ensure_initialized_is_noop_when_file_exists PASSED [ 22%]
tests/bundles/test_ledger.py::test_read_raises_loudly_when_file_is_missing PASSED [ 33%]
...（共 9 passed）
============================== 9 passed in 0.03s ===============================
```

门禁四关全绿(32 passed, 1 skipped)。架构测试 `test_only_the_ledger_module_writes_files` 仍过——`ensure_initialized` 走 `write()`,没有新增写文件点。CHANGELOG Task 4 条目已追加。无偏离。

---

### Task 5:门控状态机

**执行记录**(程序员填)

测试输出(Step 2 确认失败):
```
$ uv run pytest tests/bundles/test_gate.py -v
...
tests/bundles/test_gate.py:4: in <module>
    from bundles.memory.gate import Gate
E   ModuleNotFoundError: No module named 'bundles.memory.gate'
ERROR tests/bundles/test_gate.py
=============================== 1 error in 0.05s ===============================
```

测试输出(Step 5 确认通过):
```
$ uv run pytest tests/bundles/test_gate.py -v
...
tests/bundles/test_gate.py::test_user_stated_proposal_passes_immediately PASSED [  8%]
tests/bundles/test_gate.py::test_untrusted_proposal_waits_for_approval PASSED [ 16%]
tests/bundles/test_gate.py::test_untrusted_proposal_never_reaches_ledger_before_approval PASSED [ 25%]
tests/bundles/test_gate.py::test_resolve_approve_then_settle_writes_ledger PASSED [ 33%]
tests/bundles/test_gate.py::test_resolve_reject_drops_proposal PASSED    [ 41%]
tests/bundles/test_gate.py::test_settle_is_batched_into_single_snapshot PASSED [ 50%]
tests/bundles/test_gate.py::test_settle_is_idempotent PASSED             [ 58%]
tests/bundles/test_gate.py::test_add_goes_under_requested_section PASSED [ 66%]
tests/bundles/test_gate.py::test_amend_replaces_matched_text PASSED      [ 75%]
tests/bundles/test_gate.py::test_retire_removes_matched_line PASSED      [ 83%]
tests/bundles/test_gate.py::test_stale_amend_is_dropped_without_blocking_batch PASSED [ 91%]
tests/bundles/test_gate.py::test_settle_captures_manual_edit_first PASSED [100%]
============================== 12 passed in 0.04s ===============================
```

与计划的偏离:
- **`gate.py` `unsettled_count` 的 `fetchone()[0]` 类型(计划代码错,与前序任务同类)**:typeshed 标 `int|None`,
  mypy 严格档 `warn_return_any` 拦截。用 `int(...)` 包一层。
- **`datetime.UTC` 替代 `timezone.utc`(工具为准)**:ruff UP017,与前序任务同类。
- **`gate.py` / `test_gate.py` 多参数 keyword 调用被 ruff format 拆成每行一个**:100 字符行宽下超长,
  纯格式,无逻辑变化。

**验收结论**(Claude 填)

(待验收)

---

## M1 交付验收

全部满足才算 M1 完成:

- [ ] 12 个任务全部「通过」
- [ ] 门禁四关全绿;`uv run pytest` 预期 86 passed(不含 `test_architecture.py` 的 4 条)
- [ ] 全程无 `--no-verify` 提交
- [ ] PLAN.md Task 12 Step 4 的五项真实 API 冒烟通过,终端输出贴在下方
- [ ] 第二轮起 `[cache]` 命中 token 数 > 0

**真实 API 冒烟输出**(程序员粘贴):

```
(待填)
```

**M1 验收结论**(Claude 填):待定
