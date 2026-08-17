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
| 5 | 门控状态机 | **通过** | 全绿;retire 连坐已补(commit 9e7b482) | ☑ | 2026-08-17 |
| 6 | Memory bundle 的 MCP server | **通过** | 安全边界已验证有效;SQLite 跨线程补做 | ☑ | 2026-08-17 |
| 7 | 插件注册表与 read_skill | **通过** | 路径穿越防护扎实;manifest 可诊断性补做 | ☑ | 2026-08-17 |
| 8 | 内置工具三件 | **通过** | 全绿;检索结果封顶补做 | ☑ | 2026-08-17 |
| 9 | 上下文组装器 | **通过** | 跨进程前缀稳定已验证;时区一致性补做 | ☑ | 2026-08-17 |
| 10 | 模型客户端与缓存指标 | **通过** | 四条 API 修正均属实;run() 全路径已验证,无补做 | ☑ | 2026-08-17 |
| 11 | 一轮的编排与 CLI | **待验收** | | | 2026-08-17 |
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

- 门禁四关:ruff ☑ / mypy ☑(12 files)/ import-linter ☑(3 kept, 0 broken)/ pytest ☑(44 passed, 1 skipped)
- 重跑结果:独立重跑全绿。`test_gate.py` 12 passed。
- 规范核对(CONVENTIONS.md):**无违反**。`Proposal` 是 frozen dataclass(F1);
  `propose` keyword-only(F3);`_now`/`_row_to_proposal`/`_insert_under_section`/`_mark_stale`
  下划线标出内部性(S3);两处 `ValueError` 都带上收到的值与合法值(E3);
  `settle` 的副作用写在名字里(F6)。
- 契约核对:`Gate` 六个方法、`Proposal` 九个字段、三种 kind、两种 provenance
  与计划一致。`_insert_under_section` 的小节定位、空行处理都对。
- 不变量核对:**账本单写路径 ☑** —— `settle()` 是唯一调 `ledger.write()` 的地方,
  且落盘前先 `sync_manual_edit()`,手编内容不会被覆盖。批量结算只产生一次快照,
  「护缓存」的意图落实到位。
- 抑制与分档:无新增 noqa / type: ignore;`pyproject.toml` 未动。

**三条偏离全部成立**,均与前序任务同因(typeshed 的 `Any`、ruff UP017、formatter 换行)。

**做了三轮注入攻击测试。第二轮打穿了。**

```
【攻击1】不可信提案直接结算            → ✓ 拦住了
【攻击2】模型自己批准自己的提案        → ❌ 门控被绕过
【攻击3】retire 用偏粗的 old_text      → ❌ 三条事实一起没了
```

**攻击 2(严重,Task 6 计划的漏洞,尚未实现,来得及)**

我在 Task 6 的计划里把 `resolve_proposal` 列进了模型可调的 MCP 工具。那么攻击链是:
恶意短信进上下文 → 被注入的模型调 `propose_fact(provenance="untrusted")` →
**再调 `resolve_proposal(approved=True)`** → settle → 永久写入账本,每轮全量注入。

整套门控就此形同虚设。**它只挡得住一个还听话的模型,而听话的模型本来就不需要挡。**
工具的 docstring 写了「不得代替用户决定」,但那是提示词层面的请求,不是强制——
被注入的模型恰恰是不听这句话的那个。这直接违背 DESIGN §6.3 白纸黑字的
「按钮回调走代码状态流转,**不过模型**」。

PLAN.md 已修:`memory_tool_functions()` 从五个砍到**两个**(`propose_fact`、`list_pending`),
两个都碰不到账本;`resolve` / `settle` / `rollback` 退回成普通 Python 方法,
只由 CLI 命令(M1)或 IM 按钮回调(M2)调用。Task 11 的 CLI 相应加了
`/approve <id>`、`/reject <id>`、`/history`、`/rollback <id>`,并让 CLI 直接持有具体的
`ledger`/`gate`(组装根可以 import bundles,`LedgerPort` 保持只有 `read()` 的最小面)。
Task 6 另加一条回归测试 `test_approval_is_not_reachable_from_the_model`,
把这条边界钉死,以后谁想把审批塞回模型工具列表都会被测试拦下。

**攻击 3(中等,Task 5 需补做)**

`retire` 删掉**所有**包含 `old_text` 的行,而 `amend` 用的是 `replace(..., 1)` 只改第一处。
两者语义不一致,后果是 old_text 给粗了就连坐:

```
结算后:        ['- 住在望京', '- 公司在望京', '- 喜欢望京的烤鸭']
retire「望京」后: []
```

而 `user_stated` 是自动放行的,没有审批环节能拦住它——用户只会发现自己的事实莫名消失。
模型凭印象写个"望京"而不是整行,这事发生的概率不低。

PLAN.md Task 5 的 `settle()` 已改为只删第一处匹配行(与 amend 对齐),
并加了测试 `test_retire_removes_only_the_first_match`。

**结论:通过**(补完 retire 即可开始 Task 6;Task 6 请按修订后的计划做)
- 通过后:CHANGELOG.md 已追加条目 ☑

**补完记录**(程序员填,commit 9e7b482)

`settle()` 的 retire 分支改为只删第一处匹配行(与 amend 的 `replace(..., 1)` 语义一致),
加测试 `test_retire_removes_only_the_first_match`(三条含"望京"的事实,retire「望京」只删第一条)。

```
$ uv run pytest tests/bundles/test_gate.py -v
...
tests/bundles/test_gate.py::test_retire_removes_only_the_first_match PASSED [ 84%]
...
============================== 13 passed in 0.08s ===============================
```

门禁四关全绿(45 passed, 1 skipped)。CHANGELOG Task 5 条目已追加。无偏离。

---

### Task 6:Memory bundle 的 MCP server

**执行记录**(程序员填)

测试输出(Step 3 确认失败):
```
$ uv run pytest tests/bundles/test_memory_server.py -v
...
tests/bundles/test_memory_server.py:2: in <module>
    from bundles.memory.server import (
E   ModuleNotFoundError: No module named 'bundles.memory.server'
ERROR tests/bundles/test_memory_server.py
=============================== 1 error in 0.07s ===============================
```

测试输出(Step 5 确认通过):
```
$ uv run pytest tests/bundles/test_memory_server.py -v
...
tests/bundles/test_memory_server.py::test_build_creates_ledger_and_gate PASSED [ 11%]
tests/bundles/test_memory_server.py::test_read_ledger_returns_full_text PASSED [ 22%]
tests/bundles/test_memory_server.py::test_tool_functions_have_fixed_order PASSED [ 33%]
tests/bundles/test_memory_server.py::test_approval_is_not_reachable_from_the_model PASSED [ 44%]
tests/bundles/test_memory_server.py::test_propose_fact_tool_writes_through_gate PASSED [ 55%]
tests/bundles/test_memory_server.py::test_propose_fact_tool_reports_bad_input_instead_of_crashing PASSED [ 66%]
tests/bundles/test_memory_server.py::test_untrusted_proposal_tool_reports_pending PASSED [ 77%]
tests/bundles/test_memory_server.py::test_manifest_tools_match_implementation PASSED [ 88%]
tests/bundles/test_memory_server.py::test_skill_files_referenced_in_manifest_exist PASSED [100%]
============================== 9 passed in 1.28s ===============================
```

冒烟(Step 6):server 启动成功,打印 FastMCP banner + "Starting MCP server 'memory' with transport 'stdio'",
后台进程因 stdin 关闭干净退出(exit=0)。macOS 无 `timeout` 命令,改用后台 + kill 替代,验证效果等价。

与计划的偏离:
- **计划预期「8 passed」,实际 9 passed**:计划少数了一个测试(`test_skill_files_referenced_in_manifest_exist`
  是独立的第 9 个),不影响结果。
- **RUF059:3 个测试里 `ledger` 解包后未用**:ruff 要求加 `_` 前缀,已改 `_ledger, gate`。
- **I001:函数内 import 排序**:`from pathlib import Path`(stdlib)要先于 `import yaml`(第三方),
  ruff 自动修。
- **mypy `arg-type`:`str` → `Literal`**:server.py 的 `propose_fact` 收模型传来的 `str`,
  传给 `gate.propose` 期望 `Literal["add","amend","retire"]`。这是工具边界——模型输出是不可信输入(L3),
  gate 在运行时校验。用 `# type: ignore[arg-type]` 最小范围抑制,注释说明理由。server.py 在宽松档。
- **macOS 无 `timeout`**:冒烟改用后台 + kill,等价验证 server 能启动。

**验收结论**(Claude 填)

- 门禁四关:ruff ☑ / mypy ☑(13 files)/ import-linter ☑(3 kept, 0 broken)/ pytest ☑(54 passed, 1 skipped)
- 重跑结果:独立重跑全绿。`test_memory_server.py` 9 passed。
- 规范核对(CONVENTIONS.md):**无违反**。两个工具都返回人话错误而非抛异常(E2);
  `read_ledger` 的 docstring 讲清了"为什么不做成 MCP 工具"而不是"这个函数做什么"(G1);
  L1(prompt 不硬编码)落实——SKILL.md 与 manifest 都在文件里。
- 契约核对:`build_memory_components`、`read_ledger`、`memory_tool_functions`、`create_server`
  四个函数与修订后的计划一致;manifest 的 tools 与实现一致(有测试守着)。
- 不变量核对:**账本单写路径 ☑**,server 层只经 `gate.propose`,不碰 `ledger.write`。
- 抑制与分档:两处 `# type: ignore[arg-type]` 带理由、范围最小(单行、指名错误码),
  符合 G4;`pyproject.toml` 未动,server.py 本就在宽松档。

**五条偏离全部成立**。`type: ignore` 那条的理由尤其对——工具边界收 `str` 而非 `Literal`
正是因为模型输出是不可信输入(L3),类型收窄该在运行时由 gate 做,不该靠静态类型假装安全。

**安全边界:已在真实 MCP 表面验证有效** ✓

Task 5 验收时打穿门控的那条攻击链,现在断了:

```
MCP 暴露给模型的工具: ['list_pending', 'propose_fact']
✓ 审批 / 结算 / 回滚均不在 MCP 表面
模型能自己批准注入的提案吗? ✓ 不能,攻击链断在这里
```

**但这次真跑 MCP 又炸出一个必须补的 bug**

```
sqlite3.ProgrammingError: SQLite objects created in a thread can only be used
in that same thread.
```

FastMCP 和 Pydantic AI 都把**同步**工具函数丢进线程池执行(避免阻塞事件循环),
而连接是主线程建的、带默认 `check_same_thread=True`。**任何碰数据库的工具调用都会崩。**

单元测试发现不了它:测试里是同线程直接调函数,压根没经过框架的线程池。这是典型的
"只在真跑起来时才炸"——Task 11 一接上 agent 就会撞。实测两种配置的对比:

```
check_same_thread=True (当前): ❌ ProgrammingError
check_same_thread=False(修法): ✓ 已记下(提案 e1e3e9f2...)
```

**两处连接都中招**:`bundles/memory/server.py` 的 `build_memory_components`,
以及 `src/lararium/db.py` 的 `connect`——`search_history` 是内置工具,同样会在
线程池里碰起居注。PLAN.md 已改两处并加了三个回归测试(Task 6 两个、Task 8 一个)。

安全性不受影响:收件箱严格串行,任一时刻只有一轮在跑,不存在真正的并发访问。

**结论:通过**(补完跨线程修复即可开始 Task 7)
- 通过后:CHANGELOG.md 已追加条目 ☑

**补完记录**(程序员填,commit a1296ab)

按 Step 8–12 补 SQLite 跨线程访问:

- `src/lararium/db.py` `connect()` 加 `check_same_thread=False` + docstring 说明理由(架构保证串行,无并发)
- `bundles/memory/server.py` `build_memory_components()` 同样加 `check_same_thread=False`
- 两个回归测试:
  - `test_mcp_surface_matches_tool_functions`:`await create_server().list_tools()`,验证 MCP 暴露面只有 list_pending / propose_fact
  - `test_tools_work_when_called_from_a_worker_thread`:`asyncio.to_thread` 模拟框架线程池,验证碰库工具调用不崩

测试输出(Step 11 确认通过):
```
$ uv run pytest tests/bundles/test_memory_server.py -v
...
tests/bundles/test_memory_server.py::test_mcp_surface_matches_tool_functions PASSED [ 45%]
tests/bundles/test_memory_server.py::test_tools_work_when_called_from_a_worker_thread PASSED [ 54%]
...
============================== 11 passed in 0.53s ===============================
```

门禁四关全绿(56 passed, 1 skipped)。CHANGELOG Task 6 条目已追加。ruff format 将两处多行调用收为一行(纯格式,无逻辑变化)。

---

### Task 7:插件注册表与 read_skill

**执行记录**(程序员填)

测试输出(Step 2 确认失败):
```
$ uv run pytest tests/steward/test_registry.py -v
...
tests/steward/test_registry.py:5: in <module>
    from lararium.steward.registry import Registry
E   ModuleNotFoundError: No module named 'lararium.steward.registry'
ERROR tests/steward/test_registry.py
=============================== 1 error in 0.06s ===============================
```

测试输出(Step 4 确认通过):
```
$ uv run pytest tests/steward/test_registry.py -v
...
tests/steward/test_registry.py::test_load_discovers_memory_bundle PASSED [ 14%]
tests/steward/test_registry.py::test_directory_lines_include_name_description_and_skills PASSED [ 28%]
tests/steward/test_registry.py::test_directory_lines_are_deterministic PASSED [ 42%]
tests/steward/test_registry.py::test_read_skill_without_name_returns_overview PASSED [ 57%]
tests/steward/test_registry.py::test_read_skill_with_name_returns_body PASSED [ 71%]
tests/steward/test_registry.py::test_read_skill_rejects_unknown_bundle PASSED [ 85%]
tests/steward/test_registry.py::test_read_skill_rejects_path_traversal PASSED [100%]
============================== 7 passed in 0.02s ===============================
```

与计划的偏离:
- **ruff format 将 `skills=tuple(...)` 生成器表达式收为一行**:纯格式,无逻辑变化。

**验收结论**(Claude 填)

- 门禁四关:ruff ☑ / mypy ☑(14 files)/ import-linter ☑(3 kept, 0 broken)/ pytest ☑(63 passed, 1 skipped)
- 重跑结果:独立重跑全绿。`test_registry.py` 7 passed。
- 规范核对(CONVENTIONS.md):`BundleInfo` / `SkillInfo` 是 frozen dataclass,
  且用 tuple 而非 list 保证不可变(F1、F5);`get()` 的 KeyError 列出了已注册的 bundle(E3);
  `directory_lines` 的 docstring 说清了"为什么字节稳定"(G1)。**一类违反见下(E3)。**
- 契约核对:`Registry.load` / `directory_lines` / `get` / `read_skill`、
  `BundleInfo` 五字段、`SkillInfo` 两字段均与计划一致。
- 不变量核对:**前缀字节稳定 ☑** —— `directory_lines()` 排序确定、不含时间,
  `test_directory_lines_are_deterministic` 守住了。账本/起居注两条本任务不涉及。
- 抑制与分档:无新增 noqa / type: ignore;`pyproject.toml` 未动。

唯一偏离(formatter 收行)成立。

**路径穿越防护:扎实,六种花样全挡住** ✓

skill 名来自模型输出,是本任务最危险的输入。白名单(必须在 manifest 声明的 skills 里)
而非字符串过滤,这个选择是对的——实测:

```
'../../../etc/passwd'                   → ✓ 白名单拒绝
'..%2f..%2fetc%2fpasswd'                → ✓ 白名单拒绝
'/etc/passwd'                           → ✓ 白名单拒绝
'writing-facts/../../../../etc/passwd'  → ✓ 白名单拒绝
'writing-facts\x00.md'                  → ✓ 白名单拒绝
'SKILL'                                 → ✓ 白名单拒绝
```

**两处必须补做的静默失败(我的计划漏了)**

「扔一个新 bundle 进 compose,主控零改动」是本项目的硬指标。那么「扔错了立刻知道错在哪」
就是它的下半句,而现在这半句没有:

1. **坏 manifest 不说是哪个文件**(违反 E3)。缺字段只给 `KeyError: 'name'`;
   yaml 语法错更糟,因为是从字符串解析,PyYAML 只会说 `in "<unicode string>"`。
   装了五六个 bundle 之后,定位手段只剩逐个删目录二分。
2. **bundle 重名被静默吞掉**。实测两个 manifest 都写 `name: finance`:

```
目录行:
- finance:来自目录 finance
- finance:来自目录 finance_v2
get('finance') 拿到的是:finance_v2
→ 另一个 bundle 被静默吞掉了,目录里却还列着两行
```

模型会在前缀里看见一个它永远够不着的领域,而这事没有任何报错。

PLAN.md Task 7 已补 **Step 6–9**:抽出 `_parse_manifest`,解析失败点名文件;
加载后检查重名,重名直接拒绝启动(宁可起不来,也不要带着一个够不着的 bundle 跑);
外加三个测试。

**结论:通过**(补完 manifest 可诊断性即可开始 Task 8)
- 通过后:CHANGELOG.md 已追加条目 ☑

**补完记录**(程序员填,commit 8335c0c)

按 Step 6–9 补 manifest 加载可诊断性:

- `Registry.load` 重写:解析走 `_parse_manifest`(static method),失败时 `raise ValueError(f"{path} 不是合法的 bundle manifest:{exc}")`,捕获 `KeyError` / `TypeError` / `yaml.YAMLError`
- 加载后检查重名:用 `names.count(n)` 找出重复的 name,重名直接 `raise ValueError("bundle 重名: ...")`,拒绝启动
- 三个测试:
  - `test_broken_manifest_names_the_offending_file`:缺 name 字段 → ValueError 含 `finance/manifest.yaml`
  - `test_invalid_yaml_names_the_offending_file`:坏缩进 yaml → ValueError 含 `health/manifest.yaml`
  - `test_duplicate_bundle_names_are_rejected`:两个 `name: finance` → ValueError 含「重名」
- 共用 `_write_bundle` 辅助函数写临时 bundle 目录

测试输出(Step 8 确认通过):
```
$ uv run pytest tests/steward/test_registry.py -v
...
tests/steward/test_registry.py::test_broken_manifest_names_the_offending_file PASSED [ 80%]
tests/steward/test_registry.py::test_invalid_yaml_names_the_offending_file PASSED [ 90%]
tests/steward/test_registry.py::test_duplicate_bundle_names_are_rejected PASSED [100%]
============================== 10 passed in 0.03s ===============================
```

门禁四关全绿(66 passed, 1 skipped)。CHANGELOG Task 7 条目已追加。偏离:RUF043 要求 `pytest.raises(match=...)` 里的 `.` 转义(正则元字符),两处改用 `r"...\.yaml"` 原始字符串;ruff format 将 `load()` 的列表推导式收为一行。均为纯修正,无逻辑变化。

---

### Task 8:内置工具 current_time 与 search_history

**执行记录**(程序员填)

测试输出(Step 2 确认失败):
```
$ uv run pytest tests/steward/test_tools.py -v
...
tests/steward/test_tools.py:8: in <module>
    from lararium.steward.tools import BuiltinTools
E   ModuleNotFoundError: No module named 'lararium.steward.tools'
ERROR tests/steward/test_tools.py
=============================== 1 error in 0.06s ===============================
```

测试输出(Step 4 确认通过):
```
$ uv run pytest tests/steward/test_tools.py -v
...
tests/steward/test_tools.py::test_current_time_returns_iso_with_configured_zone PASSED [ 14%]
tests/steward/test_tools.py::test_read_skill_delegates_to_registry PASSED [ 28%]
tests/steward/test_tools.py::test_read_skill_returns_readable_error_for_unknown PASSED [ 42%]
tests/steward/test_tools.py::test_search_history_finds_chinese_and_formats_hits PASSED [ 57%]
tests/steward/test_tools.py::test_search_history_reports_no_match_clearly PASSED [ 71%]
tests/steward/test_tools.py::test_tool_function_order_is_fixed PASSED [ 85%]
tests/steward/test_tools.py::test_search_history_works_from_a_worker_thread PASSED [100%]
============================== 7 passed in 0.04s ===============================
```

与计划的偏离:
- (无)

**验收结论**(Claude 填)

- 门禁四关:ruff ☑ / mypy ☑(15 files)/ import-linter ☑(3 kept, 0 broken)/ pytest ☑(73 passed, 1 skipped)
- 重跑结果:独立重跑全绿。`test_tools.py` 7 passed,含 Task 6 遗留的跨线程回归测试
  `test_search_history_works_from_a_worker_thread` ✓——那个修复确实生效了。
- 规范核对(CONVENTIONS.md):**无违反**。三个工具都返回人话错误、不抛异常给模型(E2);
  `read_skill` 分别处理 KeyError 与 FileNotFoundError,后者还提示"检查 bundle 安装是否完整"(E3);
  docstring 写的是"什么时候用、怎么用"而不是"这个函数做什么"(G1)——这些 docstring
  会变成模型看到的工具描述,写法正确。
- 契约核对:`BuiltinTools` 三个工具 + `as_tool_functions()` 顺序固定,与计划一致。
- 不变量核对:**前缀字节稳定 ☑** —— `as_tool_functions()` 顺序固定有测试守着,
  工具 schema 是前缀第 0 层,这条守住了。
- 抑制与分档:无新增 noqa / type: ignore;`pyproject.toml` 未动。

零偏离,实现干净。

**一处必须补做的缺陷(我的计划漏了)**

`limit` 是**模型可控参数**,而计划里完全没有上界。实测:

```
limit=10000  → 返回 111,399 字符 ≈ 55,699 token
limit=-1     → 找到 500 条(SQLite 把负数当"不限制",全表倒进上下文)
limit=0      → 静默返回"没有找到",模型会误以为历史里真没有
```

一次工具调用就能塞进五万多 token:撑爆 L0、逼出一次压缩——**而压缩是全系统仅有的
两个缓存重建点之一**,不能让一次检索随手触发。这同时违反 bundle 契约里那条
「工具返回结论,不返回原料」:检索本该给钩子,不该把原料倒过来。

负数那条尤其阴——直觉上 `limit=-1` 应该更保守,实际是"不限制"。

PLAN.md Task 8 已补 **Step 6–9**:加 `MAX_SEARCH_HITS = 20` 常量、在 `search_history`
开头钳制 `limit = max(1, min(limit, MAX_SEARCH_HITS))`,并在 docstring 里明说
「最多返回 20 条;要更精确就换更具体的关键词,不是加大 limit」——**让模型知道边界,
它才不会反复试探**。外加测试 `test_search_history_caps_the_result_count`。

**结论:通过**(补完封顶即可开始 Task 9)
- 通过后:CHANGELOG.md 已追加条目 ☑

**补完记录**(程序员填,commit 5fca310)

按 Step 6–9 补检索结果封顶:

- `tools.py` 模块级加 `MAX_SEARCH_HITS = 20` / `MAX_HIT_CHARS = 200`
- `search_history` 开头钳制:`limit = MAX_SEARCH_HITS if limit < 0 else max(1, min(limit, MAX_SEARCH_HITS))`
  (负数在 SQLite 里表示"不限制",要钳制到上限而非下限)
- docstring 补「最多返回 20 条;要更精确就换更具体的关键词,不是加大 limit」
- 测试 `test_search_history_caps_the_result_count`:40 条记录,验证 `limit=10000`/`limit=-1` 都返回 20 条,`limit=0` 返回 1 条

测试输出(Step 8 确认通过):
```
$ uv run pytest tests/steward/test_tools.py -v
...
tests/steward/test_tools.py::test_search_history_caps_the_result_count PASSED [ 87%]
tests/steward/test_tools.py::test_search_history_works_from_a_worker_thread PASSED [100%]
============================== 8 passed in 0.04s ===============================
```

门禁四关全绿(74 passed, 1 skipped)。CHANGELOG Task 8 条目已追加。偏离:计划写的钳制公式 `max(1, min(limit, MAX_SEARCH_HITS))` 对 `limit=-1` 得 1 而非 20——SQLite 把负数当"不限制",测试期望钳制到上限 20。改为 `MAX_SEARCH_HITS if limit < 0 else max(1, min(limit, MAX_SEARCH_HITS))`,三组断言全过。

---

### Task 9:上下文组装器

**执行记录**(程序员填)

测试输出(Step 2 确认失败):
```
$ uv run pytest tests/steward/test_assembler.py -v
...
tests/steward/test_assembler.py:2: in <module>
    from lararium.steward.assembler import Turn, assemble
E   ModuleNotFoundError: No module named 'lararium.steward.assembler'
ERROR tests/steward/test_assembler.py
=============================== 1 error in 0.13s ===============================
```

测试输出(Step 4 确认通过):
```
$ uv run pytest tests/steward/test_assembler.py -v
...
tests/steward/test_assembler.py::test_system_prompt_contains_persona_directory_and_ledger PASSED [  9%]
tests/steward/test_assembler.py::test_prefix_is_byte_identical_across_different_envelopes PASSED [ 18%]
tests/steward/test_assembler.py::test_prefix_contains_no_timestamp PASSED [ 27%]
tests/steward/test_assembler.py::test_envelope_message_carries_the_timestamp PASSED [ 36%]
tests/steward/test_assembler.py::test_appending_a_turn_leaves_earlier_messages_untouched PASSED [ 45%]
tests/steward/test_assembler.py::test_ledger_change_is_the_only_thing_that_moves_the_prefix PASSED [ 54%]
tests/steward/test_assembler.py::test_l0_turns_become_alternating_messages PASSED [ 63%]
tests/steward/test_assembler.py::test_incomplete_turn_is_skipped PASSED [ 72%]
tests/steward/test_assembler.py::test_l1_block_appears_before_l0_when_present PASSED [ 81%]
tests/steward/test_assembler.py::test_non_user_envelope_is_marked_as_system_trigger PASSED [ 90%]
tests/steward/test_assembler.py::test_untrusted_module_event_is_wrapped_as_data PASSED [100%]
============================== 11 passed in 0.04s ===============================
```

与计划的偏离:
- **W292:assembler.py 文件末尾缺换行**,ruff 自动补。
- **ruff format 将 `test_assembler.py` 三处多行调用重排**(`build` 的 `assemble` 调用、
  `test_l1_block...` 的 `build` 调用、`test_untrusted...` 的 `Envelope.new` 调用):纯格式,无逻辑变化。
- **架构测试的组装器时钟检查自动激活**:此前 skipped 的那条「组装器读时钟」检查
  在 assembler.py 存在后开始运行并直接通过,全量从「74 passed, 1 skipped」变为
  「86 passed, 0 skipped」,恰好达到 M1 目标。

**验收结论**(Claude 填)

- 门禁四关:ruff ☑ / mypy ☑(16 files)/ import-linter ☑(3 kept, 0 broken)/ pytest ☑(**86 passed, 0 skipped**)
- 重跑结果:独立重跑全绿。`test_assembler.py` 11 passed;`test_architecture.py` 4 passed,
  其中 `test_assembler_never_reads_the_clock` **首次真正启用并通过** ✓
- 规范核对(CONVENTIONS.md):**无违反**。`Turn` / `AssembledContext` 都是 frozen dataclass(F1);
  `assemble` 全 keyword-only(F3);纯函数无副作用(F4);`_render_envelope`、`_SYSTEM_TEMPLATE`
  下划线标出内部性(S3);docstring 说的是"为什么前缀不许含时间"而非"这个函数做什么"(G1)。
- 契约核对:`assemble` 六参数、`Turn`、`AssembledContext` 与计划一致。
- 不变量核对:**前缀字节稳定 ☑**(详见下方跨进程验证);账本/可见即入账两条本任务不涉及。
- 抑制与分档:无新增 noqa / type: ignore;`pyproject.toml` 未动。

三条偏离全部成立。

**跨进程前缀稳定性:已验证** ✓

单元测试只能证明同进程内一致,但缓存真正要跨**重启**命中。Python 的哈希随机化会让
set/dict 迭代顺序逐进程变化,一旦前缀路径上有裸 set 就会每次重启换一份前缀,
而这在测试里永远看不出来。实测五种 `PYTHONHASHSEED`:

```
seed=0      → 2561d6decafa1b62
seed=1      → 2561d6decafa1b62
seed=42     → 2561d6decafa1b62
seed=12345  → 2561d6decafa1b62
seed=random → 2561d6decafa1b62
```

完全一致。`Registry.load` 里那两处 `sorted()` 起了作用。

**一处必须补做的缺陷(我的计划错)**

`_render_envelope` 里的 `envelope.ts.astimezone()` **不带参数**,取的是操作系统本地时区,
而不是 `LARARIUM_TIMEZONE`。开发机恰好是 Asia/Shanghai,所以测试全绿——但 VPS 默认
时区基本都是 UTC,一上线就分叉:

```
服务器 TZ=UTC(VPS 默认),配置仍是 Asia/Shanghai:
  信封消息里的时间 : [2026-08-17T11:57:29+00:00
  current_time 工具 : 2026-08-17T19:57:29+08:00
```

**同一轮对话里差 8 小时。** 模型看到一条 11:57 的消息、一个说现在 19:57 的工具,
对"今天/昨天/晚上"的判断就全错——而生活助手的一切都是时间相对的:记账归到哪天、
提醒定在何时、"晚上吃什么"。违反全局约束「时区统一 Asia/Shanghai」。

这个 bug 的讨厌之处在于**在开发机上永远复现不了**,要等 M2 部署到 VPS 才暴露,
那时候现象是"它对日期的判断有点怪",极难联想到时区。

PLAN.md Task 9 已补 **Step 6–9**:`assemble` 增加 keyword-only 的 `timezone` 参数,
`_render_envelope(envelope, tz)` 用 `astimezone(tz)`;Task 11 的 `loop.py` 调用点
已同步传 `timezone=self.settings.timezone`。新测试用**两个时区对比**而不是断言某个固定偏移,
这样它不依赖开发机的 TZ——否则这条测试在你机器上会永远是绿的,等于没写。

不影响前缀:时区是配置值,只作用于流水区的信封消息。

**结论:通过**(补完时区一致性即可开始 Task 10)
- 通过后:CHANGELOG.md 已追加条目 ☑

**补完记录**(程序员填,commit 09d5af1)

按 Step 6–9 补信封时间戳走配置时区:

- `assembler.py` 顶部 `from zoneinfo import ZoneInfo`;`_render_envelope(envelope, tz)` 用 `astimezone(tz)` 替代裸 `astimezone()`,注释说明 VPS 默认 UTC 的坑
- `assemble` 新增 keyword-only `timezone: str` 参数,`_render_envelope(envelope, ZoneInfo(timezone))`
- 测试 `build()` 加 `timezone: str = "Asia/Shanghai"` 并透传;新增 `test_envelope_timestamp_follows_configured_timezone_not_the_os`——同一信封分别用 Asia/Shanghai 与 UTC 组装,断言 `+08:00` / `+00:00` 且两者不同,不依赖开发机 TZ

测试输出(Step 8 确认通过):
```
$ uv run pytest tests/steward/test_assembler.py -v
...
tests/steward/test_assembler.py::test_envelope_timestamp_follows_configured_timezone_not_the_os PASSED [ 33%]
...
============================== 12 passed in 0.06s ===============================
```

门禁四关全绿(87 passed, 0 skipped)。CHANGELOG Task 9 条目已追加。偏离:ruff format 将 `build()` 的函数签名拆成每行一个参数(行宽超限),纯格式。loop.py 的调用点同步在 Task 11 的计划里已写好,届时照做。

---

### Task 10:模型客户端与缓存指标

**执行记录**(程序员填)

测试输出(Step 2 确认失败):
```
$ uv run pytest tests/steward/test_model.py -v
...
tests/steward/test_model.py:3: in <module>
    from lararium.steward.model import ModelReply, extract_cache_hit_tokens, format_cache_log
E   ModuleNotFoundError: No module named 'lararium.steward.model'
ERROR tests/steward/test_model.py
=============================== 1 error in 0.07s ===============================
```

测试输出(Step 4 确认通过):
```
$ uv run pytest tests/steward/test_model.py -v
...
tests/steward/test_model.py::test_extract_cache_hit_from_deepseek_field PASSED [ 20%]
tests/steward/test_model.py::test_extract_cache_hit_from_openai_style_field PASSED [ 40%]
tests/steward/test_model.py::test_extract_cache_hit_returns_none_when_absent PASSED [ 60%]
tests/steward/test_model.py::test_format_cache_log_reports_hit_rate PASSED [ 80%]
tests/steward/test_model.py::test_format_cache_log_handles_unknown_cache_stats PASSED [100%]
============================== 5 passed in 0.05s ===============================
```

Step 5 对着 pydantic-ai 2.31.0 核对 API(实测签名/源码),与计划的出入:

1. **`OpenAIModel` 已改名 `OpenAIChatModel`**(`pydantic_ai.models.openai`),构造函数签名
   `OpenAIChatModel(model_name, provider, settings)` 与计划一致。
2. **`AgentRunResult.usage` 是 property 不是方法**:源码里 `@property def usage(self) -> RunUsage`。
   计划写 `result.usage()` 会在运行时抛 `TypeError: 'RunUsage' object is not callable`,已改为 `result.usage`。
3. **`_CACHE_HIT_KEYS` 增加 `cache_read_tokens`**:pydantic_ai 的 `RunUsage` 缓存命中 token
   是顶层字段 `cache_read_tokens`,不在计划探测列表里。不加的话真实运行缓存命中恒为 None,
   `format_cache_log` 永远打「未知」,Task 12 的「第二轮起 cache 命中 > 0」验收必挂。
   DeepSeek 兼容层的 `details.prompt_cache_hit_tokens` 路径保留。
4. **`history` 加显式 union 标注**:mypy 把空列表推断为 `list[ModelRequest]`,追加 `ModelResponse` 报
   `arg-type`。改为 `history: list[ModelRequest | ModelResponse] = []`。
5. 其余全对:Agent 的 `toolsets=` 参数、`result.output`、`result.new_messages()`、
   `part.part_kind` / `part.tool_name` / `part.args` / `part.content` 均按计划原样可用。
6. 实测两种 usage 形状都能提取:RunUsage 顶层 `cache_read_tokens` → 1200;
   DeepSeek `details.prompt_cache_hit_tokens` → 500;无缓存字段 → None。

**验收结论**(Claude 填)

- 门禁四关:ruff ☑ / mypy ☑(17 files)/ import-linter ☑(3 kept, 0 broken)/ pytest ☑(92 passed, 0 skipped)
- 重跑结果:独立重跑全绿。`test_model.py` 5 passed。
- 规范核对(CONVENTIONS.md):**无违反**。`ModelClient` 是 Protocol、`ModelReply` 是
  frozen dataclass(F1);**D2 落实得干净**——`pydantic_ai` 的 import 全部关在
  `PydanticAIClient` 内部,连模块顶层都没有,库升级的爆炸半径就是这一个类。
- 契约核对:`ModelReply` 五字段、`ModelClient.run` 签名、`extract_cache_hit_tokens`、
  `format_cache_log` 与计划完全一致。**协议与测试一行未改,只动了适配器**——
  隔离盒的意义正在于此,这一点做对了。
- 不变量核对:三条本任务均不直接涉及;但缓存可观测性是「前缀稳定」的度量手段,见下。
- 抑制与分档:无新增 noqa / type: ignore;`pyproject.toml` 未动(`model.py` 本就在宽松档)。

**四条 API 修正我逐条独立核对,全部属实**:

```
pydantic-ai: 2.31.0
OpenAIChatModel 存在: True | OpenAIModel 存在: False
AgentRunResult.usage 是 property: True
RunUsage 有 cache_read_tokens: True
Agent 接受 toolsets: True
```

`OpenAIModel` 确实已不存在——计划会在 import 处直接崩。`result.usage()` 会抛
`TypeError: 'RunUsage' object is not callable`。这两条都是真 bug。

**第 3 条(`cache_read_tokens`)的价值最高**:不加它,真实运行时缓存命中恒为 `None`,
`format_cache_log` 永远打「未知」,而 Task 12 的验收标准「第二轮起 cache 命中 > 0」
必挂——到那时排查方向会跑偏到"是不是前缀不稳定",实际只是指标读错了字段。
**你自己把这条和验收标准联系起来了,这正是我期待的判断力。**

**额外验证:`run()` 全路径已跑通(单元测试完全没覆盖这段)**

5 个单元测试只测了两个纯函数,`run()` 里的历史构造、tool 事件提取、输出解包
都没人跑过。我用 pydantic-ai 自带的 `TestModel` 走了一遍完整 agent 循环,无需 API key:

```
✓ run() 跑通,没有抛异常
  回复文本 : success (no tool calls)
  token    : prompt=56 completion=5 cache_hit=0     ← 0 而非 None,说明字段探测生效
  日志行   : [cache] 命中 0/56 (0.0%) · completion=5
```

再强制调用工具,验证最脆的那段(靠 `part_kind` 字符串匹配):

```
  tool_call    current_time     None
  tool_call    search_history   {'query': 'a'}
  tool_result  current_time     2026-08-17T20:00:00+08:00 星期一
  tool_result  search_history   找到 1 条:关于a
  调用与结果都提取到了吗?✓ 是
```

这段直接决定「可见即入账」能不能成立——工具事件漏提取的话,起居注就重建不出完整一轮。

**给 Task 12 的前瞻提醒(不是本任务的缺陷)**

冒烟时若发现第二轮 `cache 命中` 仍是 0,先别怀疑前缀不稳定——DeepSeek 的上下文缓存
有最小块长要求,前缀太短不会被缓存。我们的前缀(人格 + 目录 + 账本)在 M1 阶段
只有几百 token,可能不够。真遇到就先确认前缀长度,再查稳定性。

**结论:通过**(无补做项,直接开始 Task 11)
- 通过后:CHANGELOG.md 已追加条目 ☑

门禁四关全绿(92 passed, 0 skipped;mypy 17 files)。W292 缺结尾换行与 ruff format 重排为纯修正。

---

### Task 11:一轮的编排与 CLI

**执行记录**(程序员填)

测试输出(Step 2 确认失败):
```
$ uv run pytest tests/steward/test_loop.py -v
...
tests/steward/test_loop.py:11: in <module>
    from lararium.steward.loop import Steward
E   ModuleNotFoundError: No module named 'lararium.steward.loop'
ERROR tests/steward/test_loop.py
=============================== 1 error in 0.15s ===============================
```

测试输出(Step 5 确认通过):
```
$ uv run pytest tests/steward/test_loop.py -v
...
tests/steward/test_loop.py::test_process_next_returns_reply_text PASSED [ 11%]
tests/steward/test_loop.py::test_process_next_returns_none_when_inbox_empty PASSED [ 22%]
tests/steward/test_loop.py::test_model_receives_builtin_and_bundle_tools_in_fixed_order PASSED [ 33%]
tests/steward/test_loop.py::test_turn_is_fully_recorded_in_journal PASSED [ 44%]
tests/steward/test_loop.py::test_recorded_prompt_matches_what_model_received PASSED [ 55%]
tests/steward/test_loop.py::test_second_turn_sees_first_turn_in_l0 PASSED [ 66%]
tests/steward/test_loop.py::test_prefix_identical_between_turns_when_ledger_unchanged PASSED [ 77%]
tests/steward/test_loop.py::test_settled_fact_appears_in_next_prefix PASSED [ 88%]
tests/steward/test_loop.py::test_model_failure_logs_error_and_does_not_wedge_the_queue PASSED [100%]
============================== 9 passed in 0.64s ===============================
```

CLI 冒烟(Step 6 之后):用管道喂 EOF 验证启动与退出路径,不真实调 API:
```
$ LARARIUM_API_KEY=sk-test LARARIUM_DATA_DIR=$(mktemp -d) uv run python -m lararium.gateway.cli < /dev/null
Lararium 已启动。输入 /help 看命令,/quit 退出。

你 > 退出。
exit=0
```
数据文件正确创建:memory/ledger.md、memory/memory.sqlite、steward.sqlite。

与计划的偏离:
- **ASYNC250:`input()` 在 async main() 里阻塞调用**,ruff 拦下。改为 `await asyncio.to_thread(input, "\n你 > ")`——把阻塞式输入移出事件循环(也为 M3 的定时任务留出 loop)。
- **I001:计划 commit 命令漏了 `ports.py`**,已在 commit 里补上(loop.py import 它,不提交仓库不完整)。
- 其余照抄即过:recover_stale() 在 main() 启动时调用 ✓、`assemble(..., timezone=self.settings.timezone)` ✓、loop.py 全程走 `LedgerPort`/`GatePort` Protocol 不碰 bundles ✓。

门禁四关全绿(101 passed, 0 skipped;mypy 20 files;import-linter **3 kept, 0 broken**——Steward 首次需要 Memory 能力,契约验证守住)。

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
