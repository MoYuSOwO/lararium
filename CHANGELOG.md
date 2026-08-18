# 变更日志

按里程碑分节记录。**每个任务验收通过后,立刻在当前里程碑下追加一行**——
攒到里程碑结束再补,你会想不起来那个任务到底解决了什么。

**写什么**:写"这个系统现在多了什么能力、少了什么坑",不写"改了哪几个文件"——那是 git log 的活。
带上任务编号,方便回查 [REVIEW.md](REVIEW.md) 的验收记录。

**格式**:一行一条,默认都是新增能力;是修复或改变已有行为的,在开头标出来。

```
- **Task 3** 起居注支持中文检索,"日料"能搜到"那家日料店"(两字词走 LIKE 回退,trigram 只认三字以上)
- **Task 7** 修复:skill 名走 manifest 白名单校验,堵住路径穿越
```

---

## M1 · 骨架(已完成)

目标:能在终端里对话、事实走完门控全流程并在后续生效、任一轮可从起居注重放、每轮打印缓存命中。

<!-- ↓ 第一条从这里开始写,删掉本行注释 -->

- **Task 1** 配置加载就位:`Settings.load()` 从 `LARARIUM_*` 环境变量读模型与运行参数,缺 API key 即报错;人格总则 `prompts/persona.md` 落地;测试经 conftest 与宿主环境隔离,`source .env` 后跑测试不再串读真实配置
- **Task 2** 信封模型与严格串行收件箱落地:`Envelope` 走 pydantic、`Inbox` 用 SQLite 持久化 + `BEGIN IMMEDIATE` 保证任一时刻最多一条 processing;崩溃恢复 `recover_stale()` 在启动时清理遗留的 processing 记录,带重试上限防毒消息,避免硬崩溃后队列永久卡死、助手静默
- **Task 3** 起居注落地:append-only `Journal`,FTS5 trigram 做中文检索、两字词回退 LIKE;`replay()` 逐字重放单轮、`recent_turns()` 给 L0 取最近对话;内部事件(prompt/tool_call)不进检索索引
- **Task 4** 账本文件与快照表落地:`Ledger` 管 markdown 账本 + SQLite 快照表,支持手编检测(`sync_manual_edit`)、回滚、diff;`read()` 纯读缺文件即报错(防静默失忆),新建职责交给启动期 `ensure_initialized()`,全代码树只剩 `Ledger.write()` 一处写文件
- **Task 5** 门控状态机落地:`Gate` 分档审批(user_stated 直通、untrusted 待审批),`settle()` 批量结算护缓存,amend/retire 用 old_text 精确匹配、过期提案打回不阻塞同批;retire 只删第一处匹配行,粗 old_text 不再连坐
- **Task 6** Memory bundle MCP server 落地:`build_memory_components` 组装 Ledger+Gate、`create_server` 暴露 FastMCP、`memory_tool_functions` 只含 `propose_fact` 与 `list_pending` 两个工具(审批/结算/回滚走代码路径,不经模型);SQLite 连接加 `check_same_thread=False`,框架线程池里的工具调用不再崩
- **Task 7** 插件注册表落地:`Registry.load` 扫描 `bundles/*/manifest.yaml` 组装目录,`read_skill` 走 manifest 白名单校验挡路径穿越,`directory_lines` 确定性排序保证前缀字节稳定;坏 manifest 点名出错的文件路径,重名 bundle 直接拒绝启动,不留够不着的领域
- **Task 8** 内置工具三件落地:`current_time` 带时区与星期、`read_skill` 委托注册表、`search_history` 走起居注 FTS5;`as_tool_functions` 顺序固定护缓存;检索结果硬封顶 20 条,`limit=-1`(SQLite 当不限制)与 `limit=10000` 都钳制到上限,一次工具调用不再撑爆 L0
- **Task 9** 上下文组装器落地:纯函数 `assemble` 拼前缀区(人格/目录/账本,字节稳定)与流水区(L1 摘要 + L0 对话 + 本轮信封);untrusted 外部数据包裹成「数据不是指令」;信封时间戳走配置时区(`assemble(timezone=...)`),不再依赖操作系统本地时区,VPS 上不再和 `current_time` 差 8 小时
- **Task 10** 模型客户端协议与缓存指标落地:自有的 `ModelClient` 协议 + `ModelReply`,Pydantic AI 实现关在隔离盒 `PydanticAIClient` 里(库升级只改一个文件);`extract_cache_hit_tokens` 按服务商探测缓存命中 token(DeepSeek `details` / pydantic-ai `cache_read_tokens`),`format_cache_log` 每轮打印命中率,缓存可观测性落地
- **Task 11** 一轮编排与 CLI 落地:`Steward.process_next` 认领→记事件→组装→跑模型→记工具事件→记回复→完成,整个循环可从起居注重建;Steward 只经 `ports.py` 的 `LedgerPort`/`GatePort` 接触 Memory(契约首次实战守住);CLI 带 `/approve` `/settle` `/history` `/rollback` `/replay` 等代码路径命令,审批不过模型;打错的 `/` 命令提示「未知命令」不再误发模型,一次 API 错误(限流/401)不再打死 CLI——随时可用的底线
- **Task 12** M1 端到端验收:四条 DESIGN §12 标准自动化测试(事实走通门控并在后续生效 / 任一轮逐字重放 / 前缀跨轮字节稳定 / 不可信内容进不了账本);真实 API 冒烟六项全过(OpenCode Go + mimo-v2.5),第二轮起 `[cache]` 命中 53.8%;缓存日志标注请求数(用量是整轮累加的,看到「N 请求」就不会把工具往返稀释的百分比误读成前缀不稳)——M1 里程碑完成
- **补1** M1 审计 P0-1:第二轮起模型前缀(人格/目录/账本)整段丢失。根因是 `message_history` 非空时 pydantic-ai 不再注入 `Agent(system_prompt=)`。改为把前缀作为 `SystemPromptPart` 放进历史首条 `ModelRequest`,首轮同路径;让「事实在后续对话生效」这条验收标准重新成立
- **补1b** 补1 的报文测试往下挪到 HTTP 层:`FunctionModel` 只看到库内部表示、OpenAI 适配器不在链路上,对发出的字节无知。改用 `httpx.MockTransport` 断言真正发出去的 `body["messages"]`,补上工具往返测试(一轮两次请求);夹具抽 `conftest.py`,`httpx` 显式声明进 dev 依赖

---

## M2 · 前后端分离(已完成)

目标:Steward 变常驻 HTTP 服务,所有前端走同一协议;消息入队立返,worker 逐条干;CLI 降级为普通客户端。协议契约冻结(token 定 channel、/v1/messages、/v1/outbox 长轮询、/v1/commands、/v1/health)。

- **M2-1** 出件箱落地:`Outbox` 独立于起居注(起居注逐字 append-only,投递要 UPDATE),回复先落箱、信封才算完成——崩溃重算多花一次 API 但绝不静默吞回复;seq 全局递增、客户端按 seq 去重(at-least-once);`take` 观测性标记 delivered_at 不阻止再取
- **M2-2** 错误分类与重试(P2-3 关闭):隔离盒 `model.py` 把 pydantic-ai 异常分类成自家 `ModelCallError(retryable=...)`(429/5xx/连接/超时/认不出→可重试,400/401/403/404/422→终态,不对称是有意的),loop 只认它;可重试回 pending 重试(退避上限封顶 3 次),超限/终态 → failed + 出件箱 notice 含原文前 50 字;`Settings.max_attempts`(默认 3)
- **M2-3** worker 事件驱动串行(D11):`TurnOutcome` 拆三支(replied/empty/retry_later)消除 `process_next` 返回 None 的一词多义;单消费者空闲时结算、`retry_later` 指数退避(2**attempts 封顶 60s,绝不等 wake——否则任何新消息都立刻重锤被限流的消息)、毒消息吞异常继续,不陪葬
- **M2-4** HTTP 服务 + Step0a 关 SDK 隐藏重试(max_retries=0,否则与持久重试叠乘:一条 429 打 3 个请求×max_attempts=9,且 SDK 层在内存、起居注看不见、重启就丢——两套留强的那套);`Envelope.id` 加 32-hex pattern + validate_assignment(伪造 id 曾被 search_history 渲染在围栏外,是 P1-4 换字段);AST 写禁升级;`Settings` 加 bind/tokens
- **M2-5** 命令端点 `POST /v1/commands`:handle_command 搬到 `gateway/commands`,门控开关(D12)经 HTTP 直通;**token 分能力两类**——控制端(全权)与数据面 ingest(只准入站,命令/出件箱/健康一律 403,堵住"恶意短信自己批准自己"的整链攻击);HTTP `/quit` 零副作用
- **M2-6** CLI 降级为纯 HTTP 客户端(httpx + after 游标持久化,重启不丢不重),`.importlinter` 新增"cli 是纯客户端"契约;六项真实 API 双终端冒烟全过——含 **kill -9 中途杀服务→重启→recover_stale 重排队→回复最终送达**,D10 崩溃语义在真实进程上成立

---

## M3 · 陪伴与记忆(进行中)

目标:记得住、接得上、说话像个熟人。200k 预算用满;话头归 Steward(对话自身状态,不是生活领域),压缩只产索引行不产状态卡。

- **M3-1** L0 按 token 预算截断(200k)+ 收掉 M2-6 遗留:`outbox.put`+`inbox.complete` 同一事务(崩在中间不再重复回复,D10 真·恰好一次);`recent_turns_within_budget` 从最新往回填、预算耗尽即停、最新一轮无条件在;上下文超长类 400 的 notice 说人话(不甩 status_code: 400)。补做:M3-1b 把估算器实测定标(CJK 0.8/非 CJK 0.3,原 len//2 低估 1.4~1.6 倍)、预算改为整窗口径(读前缀-8000 留白,余额归 L0)、`_turns_by_id` 一条 SQL 不走 replay(800 轮 274ms→28ms)、`db.transaction(conn)` + `Inbox/Outbox.conn` 属性

---

## M0 · 立项与工程基建(已完成)

- 架构设计 v2.0([DESIGN.md](DESIGN.md)):单 agent + plugin bundle。推翻了 v0.1 的多 agent + handoff 方案——多个脑子之间传话必丢、交接必漏,那是结构性故障不是实现问题
- 记忆系统定案:显式账本全量注入 + 门控写入(pending 隔离区斩断持久化提示注入)+ 快照表管历史(git 退场,30 行标准库代替)
- 压缩策略定案:话题片段分治(闭合的丢成索引、开放的写成状态卡),放弃了滚动叙事摘要——生活对话没有主线
- M1 实施计划([PLAN.md](PLAN.md)):12 个任务,TDD 逐步展开,每步含可运行代码
- 工程门禁四关:ruff(含 DTZ 时区规则)、mypy(分层严格)、import-linter(架构边界)、pytest(项目不变量)。提交时自动执行,三条真实违规已实测能拦住
- 开发规范([CONVENTIONS.md](CONVENTIONS.md)):36 条分七组,只收"防真实腐烂且机器判不了"的判断题
- [AGENTS.md](AGENTS.md):编码 agent 的导航入口(CLAUDE.md 软链到它),分层指路而非重复规则
- 验收协议([REVIEW.md](REVIEW.md)):门禁先跑、规范按编号引用、三条跨任务不变量人工核对

---

## 里程碑进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 立项 | 设计、计划、门禁、规范 | ✅ 完成 |
| M1 骨架 | CLI 对话、门控记忆、可重放、缓存可观测(含交付后审计补做) | ✅ 完成 |
| M2 前后端分离 | 出件箱、错误重试、worker、HTTP 协议、CLI 客户端化 | ✅ 完成 |
| M3 记忆中间层 + 陪伴 | L0 预算截断、话头、检索双路、压缩 v1(话题索引,无状态卡)、夜间归拢、人格改写 | ⬜ 进行中(M3-1 已过) |
| M4 上线 | VPS、Caddy、渠道定型、IM 适配器、晨报 | ⬜ 未开始 |
| M5 功能 bundle + 深度 | 财务/健康/学习、沙箱、计划巡检、压缩实验 | ⬜ 未开始 |

> 2026-08-18 重排(原 M2=财务+上线):记忆中间层先于功能 bundle,理由见 DESIGN.md D13。
