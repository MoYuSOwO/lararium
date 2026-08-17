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

## M1 · 骨架(进行中)

目标:能在终端里对话、事实走完门控全流程并在后续生效、任一轮可从起居注重放、每轮打印缓存命中。

<!-- ↓ 第一条从这里开始写,删掉本行注释 -->

- **Task 1** 配置加载就位:`Settings.load()` 从 `LARARIUM_*` 环境变量读模型与运行参数,缺 API key 即报错;人格总则 `prompts/persona.md` 落地;测试经 conftest 与宿主环境隔离,`source .env` 后跑测试不再串读真实配置
- **Task 2** 信封模型与严格串行收件箱落地:`Envelope` 走 pydantic、`Inbox` 用 SQLite 持久化 + `BEGIN IMMEDIATE` 保证任一时刻最多一条 processing;崩溃恢复 `recover_stale()` 在启动时清理遗留的 processing 记录,带重试上限防毒消息,避免硬崩溃后队列永久卡死、助手静默
- **Task 3** 起居注落地:append-only `Journal`,FTS5 trigram 做中文检索、两字词回退 LIKE;`replay()` 逐字重放单轮、`recent_turns()` 给 L0 取最近对话;内部事件(prompt/tool_call)不进检索索引
- **Task 4** 账本文件与快照表落地:`Ledger` 管 markdown 账本 + SQLite 快照表,支持手编检测(`sync_manual_edit`)、回滚、diff;`read()` 纯读缺文件即报错(防静默失忆),新建职责交给启动期 `ensure_initialized()`,全代码树只剩 `Ledger.write()` 一处写文件
- **Task 5** 门控状态机落地:`Gate` 分档审批(user_stated 直通、untrusted 待审批),`settle()` 批量结算护缓存,amend/retire 用 old_text 精确匹配、过期提案打回不阻塞同批;retire 只删第一处匹配行,粗 old_text 不再连坐
- **Task 6** Memory bundle MCP server 落地:`build_memory_components` 组装 Ledger+Gate、`create_server` 暴露 FastMCP、`memory_tool_functions` 只含 `propose_fact` 与 `list_pending` 两个工具(审批/结算/回滚走代码路径,不经模型);SQLite 连接加 `check_same_thread=False`,框架线程池里的工具调用不再崩

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
| M1 骨架 | CLI 对话、门控记忆、可重放、缓存可观测 | 🔨 进行中 |
| M2 财务 + 上线 | 财务 bundle、ingress、VPS 部署、渠道定型 | ⬜ 未开始 |
| M3 主动性 + 压缩 | 调度器、晨报、健康 bundle、压缩 v1 | ⬜ 未开始 |
| M4 深度 + 实验 | 学习 bundle、计划巡检、压缩参数实验 | ⬜ 未开始 |
