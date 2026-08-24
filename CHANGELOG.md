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

## M3 · 陪伴与记忆(已完成)

目标:记得住、接得上、说话像个熟人。200k 预算用满;话头归 Steward(对话自身状态,不是生活领域),压缩只产索引行不产状态卡。

- **M3-1** L0 按 token 预算截断(200k)+ 收掉 M2-6 遗留:`outbox.put`+`inbox.complete` 同一事务(崩在中间不再重复回复,D10 真·恰好一次);`recent_turns_within_budget` 从最新往回填、预算耗尽即停、最新一轮无条件在;上下文超长类 400 的 notice 说人话(不甩 status_code: 400)。补做:M3-1b 把估算器实测定标(CJK 0.8/非 CJK 0.3,原 len//2 低估 1.4~1.6 倍)、预算改为整窗口径(读前缀-8000 留白,余额归 L0)、`_turns_by_id` 一条 SQL 不走 replay(800 轮 274ms→28ms)、`db.transaction(conn)` + `Inbox/Outbox.conn` 属性
- **M3-2** 话头存储(Steward 独占,非 bundle):`Threads` 表 + `open_thread`(同名 upsert)/`close_thread` 内置工具追加在既有工具之后、顺序不插队,`open_threads()` 是代码路径不占模型工具位;条数上限 5、note 字数 80。补做:topic 加 `MAX_TOPIC_LEN=24` + 归一化(折内部空白/去首尾/空名拒),close 同套归一化
- **M3-3** 话头进信封(冻结):认领后把 `open_threads()` 快照冻结进 `env.meta`,历史轮渲染**当时那份**(append-only 严格前缀回归);话头行渲染规矩(P1-2 折内部换行 / P1-3 topic+note 过 neutralize_fence / 像自己记的待办)。Step0:预算口径改「渲染后的形态」(每轮 +10 普通/+40 不可信 + 话头行),2000 轮短聊整份 ≤200k;threads 顺带(去 PMID:、截后 strip);directory/ledger read-once
- **M3-4** 检索拆两个工具 + 分页:`search_history` 词法(报总数+页)、`recall_similar` 语义(vec0+256 维本地 embedding)——决定性对照独立语料 0/5 vs 4/5,recall 存在的一切理由;低于相似度阈值(0.35)不计入总数;page 钳制;两条注入回归照抄词法路(P1-2 折行 / P1-3 中和围栏);embedding 数据面算不碰缓存。补做:扩展加载失败降级不拦启动(VEC_AVAILABLE 三处消费,recall 复用 E2 提示)+ 启动期预热 embedding(慢启动诚实,聊一半卡住不是)
- **M3-5** 夜间归拢(sweep):扫一段起居注补账——只改话头 + 提 pending(untrusted,账本单写者),模型输入/输出逐字落起居注,同区间幂等,掉出前5名的话头也能关(Threads.all_open);廉价模型可单配。补做:归拢 prompt builder 过四条渲染规矩(按来源标注外部数据不写成用户 / fold_text 折行 / 围栏+中和),_fold 提公开
- **M3-6** 压缩:上下文用满 200k 时把顶出低水位的旧轮压成 L1 索引(日期 · 话题 · 结论 · 信封id),只产索引行不产状态卡("什么还开着"是话头的活);沉淀筛直接复用 M3-5 的 Sweeper;两道审批屏障(pending 非空则停,证据销毁前必须结案);append-only 只标记退出不删正文;预算按渲染后口径再扣 L1。补做:索引钩子/日期来自模型切段(id 校验+回退)+ 走配置时区(凌晨不差天)+ worker 空闲自动触发(最小间隔)
- **M3-7** 人格改写(persona.md,一次缓存重建):说话分「何时简短/何时真对话」(账务待办查数一句话带判断 / 聊现实生活接住话题陪伴不附和)+ 入档补**变化频率轴**(三个月仍成立才入档,会变的归话头;同步进 writing-facts 判据3)。真机四样本:情绪/做菜/记账零误 propose、旧事调 search_history 去查、稳定事实(妈妈生日)propose 进账本;补做:「提醒一次就够别每轮念」+ 恢复套话禁令
- **M3-8** 文档收口 + 端到端:DESIGN §6.6(检索拆两工具/对照 0/5 vs 4/5/embedding 已定)、§7(压缩编排两屏障/触发)、§11(200k+前缀缓存)、§13(embedding 已定)回填;persona 顺带(提醒一次就够 + 恢复套话禁令);端到端 200k 连聊 30 轮(前缀零重建/严格追加/话头跟着变)+ 真模型 [cache] 60→94.5%。补做:文档四处自相矛盾清理(状态卡遗文案/开放问题删状态卡机制)+ M3 条目排序

---

## M4 · 第一个领域 bundle(财务·对话侧)

目标:能在对话里记账、能问出结论。结构价值是第一次有 memory 之外的 bundle,把 §5 的 bundle 契约从"写在文档里"变成"跑过一遍";数据面(短信自动入账)不在本里程碑。

- **M4-1** 第一个领域 bundle 骨架落地:`bundles/finance` 扔进目录即被 `Registry` 发现(**注册代码零改动**),代价是目录行 +1、工具 schema +3 的一次性前缀重建(D3 认可的重建点:注册表/工具变更 = 重启);统一构造入口 `build(data_dir) -> BundleRuntime`(`bundles/runtime.py` 只放形状不放行为——共享模块不受 independence 契约保护,放了行为 bundle 之间就开始背着契约共享东西),memory 的 ledger/gate 仍走 ports、不为形状统一被抹平(§6.1 特殊地位,多一条通往同一批组件的路 = 给「账本只有一条写入路径」加岔口);finance 独占自己的 SQLite,产权测试断言它的表零泄漏进 `steward.sqlite`;三个工具的签名与文档**一次定死**(工具 schema 是前缀第0层,骨架态函数体是 E2 人话占位、正体在 M4-2/3/4 换),组装根用显式小表把顺序钉成 `[propose_fact, list_pending, record_expense, query_spending, list_recent]`——加一个领域改一行,不改一处逻辑
- **M4-2** 记一笔落地:`record_expense` 金额走 Decimal 转**整数分**(浮点会让月度合计以「对不上一分钱」的形式冒出来,而那时候你已经查不出是哪笔);类目锁死七类(自由文本会让模型每次发明新词,「吃饭」「餐饮」「外卖」各记一笔,聚合就废了);`occurred_at` 缺省为**配置时区**的现在——时区由组装根注入而不是 bundle 自兜默认值(自兜就会和 `Settings` 各走各的,正是 M1 Task 9 那个 8 小时时差),带偏移的先折回配置时区再落库(原样存会让 SQLite 的 `date()` 按 UTC 切天,整月分组静悄悄错一天)。E2 边界四条一条都不抛:非法类目列出全部合法值、非正数金额、看不懂的时间(**不许悄悄退回"现在"**——模型说的是「上周三」,账上落成今天没有任何人会知道)、以及大到 int64 存不下的金额(补做:`OverflowError` 不是 `sqlite3.Error` 的子类,曾经直接逃出工具边界,信封被标 failed 无声死掉、模型连自我纠正的机会都没有)。**测试用变异检查验真,9 条变异 9 条被咬住**;其中金额那条第一版是绿的——`0.1*100` 在 IEEE754 里正好是 `10.0`,再叠上 SQLite 的 INTEGER 亲和性把 `10.0` 收成整数,两层一起掩护,浮点实现能大摇大摆过关,换成 `1.005` 才咬得住
- **M4-3** 查落地(工具铁律 A4 的第一次实战):`query_spending` 的 `GROUP BY` 在 SQL 里算完再返回——300 笔带唯一标记的流水进去,出来是「区间·共 N 笔·合计 X 元」+ 每组一行,标记一条都不泄漏;按类目金额降序(第一行就是吃掉预算的那一类),按天**时间正序**且整月 31 天原样装得下(上限的判据是「最常见的那个查询要装得下」,不是照抄 `MAX_SEARCH_HITS`——按金额降序会把时间轴打散,趋势就读不出来了)。超限不许静默截断:被砍掉的部分单列一行报组数和合计,总额行始终是全区间的。日期上界取**次日零点开区间**——落库是 `YYYY-MM-DDTHH:MM:SS`,闭区间会把末日带时刻的流水全吃掉,而合计只是"小了一点",没人会发现。E2 六条路径全走人话(日期反了不许假装成"没有记录")。**「聚合走 SQL 不在 Python 里算」没有任何行为测试能抓**(两种写法输出逐字相同),靠读代码守,已在 REVIEW 写明
- **M4-4** 月度复盘 skill + `list_recent`:分层路由第三层第一次有真内容(`monthly-review.md` 写方法不写数据——先看总额趋势、再看异常类目比**占比**不比绝对值、最后看单笔大额);`list_recent` 是全系统唯一返回原始流水的口子,所以硬封顶 20、`limit=-1`(SQLite 当"不限制")与超大值都钳到上限,并扩出 `since/until/order`——只加排序不给日期范围,「上个月最大的一笔」会被答成全时段之最。**还上早就登记的那笔 note 账**:`note` 是模型写的、不可信轮会把短信正文转述进去,跨轮被捞回来时是 `tool_result` 身份、坐在可信位置、围栏和来源标签全掉;渲染时统一过三刀(折行防伪造整行流水 / 中和围栏与界符防 P1-3 提前闭合 / 先折再截),`record_expense` 的回执和 `list_recent` **共用同一个渲染器**——两套渲染器正是 P1-1 的成因,测试拿同一份 note 走两个出口断言逐字相同。围栏常量是从 assembler 抄的(bundle 不许 import steward),`test_fence_markers_match_the_stewards` 把两边钉死,漂了就红
- **M4-5** 账本与流水的边界:硬边界从 skill 文件(到达率 33%)搬进**每轮都在的前缀**——persona 写明「流水进领域模块,不进账本」,`writing-facts.md` 三个判据补成四个,第四条是**单次事件 vs 稳定安排**(和"变化频率"不是一回事:一次性事件哪怕三个月后仍为真也不该入档,账本记的是他是个什么样的人、不是他做过什么)。真模型三轮 × 十笔:`propose_fact` 零次、账本逐字节未变(**在显式 settle 之后比的**——`user_stated` 当场 passed、worker 空闲自动 settle 那条路必须走一遍,否则"没变"只是因为没人结算)、房租照样 propose(不矫枉过正)。**基线也是零次,所以乙的价值是结构性的不是行为上的**
- **M4-5b/c** 记账依从率:诊断定位到根因在 **L0 的结构**——`_turns_by_id` 只回放 `envelope`/`reply`,工具事件从不回来,模型每轮看到的历史是"用户报开销 → 助手说记好了",**里面没有任何调过工具的证据**,它照着这份被裁掉工具栏的成绩单往下做(同上下文 33/100 vs 空上下文 50/50)。v1 把工具名渲染成助手正文里的一行字:37%→67%,但**模型学会了写那一行来代替调那个工具**(5/5 漏出的痕迹行零真实调用,而伪造出来的那行会存进起居注、下一轮原样回到 L0,和真痕迹逐字同形)。v2 换成**协议层原生形状**(`assistant.tool_calls` + `role:"tool"` 结果消息):位置 3-10 = 80/80,贴平同模型空上下文天花板,**可被伪造的记号根本不存在**。代价是工具结果进了 L0,所以结果与参数都过折行+中和、结果截断可见;预算实测(封装 18.5~26 取 30;工具正文另立一把尺 1.0/字符——日期金额每个数字组几乎自成一个 token,比 base64 还贵)
- **M4-5d** 重试不再重复副作用:可重试失败后 `release()` 让整轮重跑,而失败那轮**没有 reply、不进 L0**,模型对上一次的成功一无所知——一次 503 就能让一笔午饭记两条、一条事实在账本里留下永久重复(`max_attempts=3` 能记三份)。改成**按顺序回放上一次已成功的工具结果、只从断点之后真执行**,挂在工具边界包装层(对所有 bundle 成立,不必每模块各写一遍);**不按 (工具名,参数) 去重**——用户真报两笔一模一样的午饭是合法的,去重会吃掉第二笔,有反向测试钉住。顺带补上一个前置缺口:工具执行原来只在 `model.run` **成功返回后**才落起居注,失败那轮的副作用一条记录都没有,现在由包装层在执行点记 `tool_executed`(带 `replayed` 标志,查重复记账时分得清哪次真跑过)
- **M4-6** M4 收口:自动化端到端(记账→查询→读 skill 五轮走通)钉住两条不变量——**前缀区跨全程零变化**、**L0 严格追加**;真机五项按 PLAN 原措辞全过:该记账的走 `record_expense` 且 `propose_fact` 零次、「房租每月 3800」该入档还是入档且没同时记成流水、查询给结论不给流水、类目没被乱发明,「这个月最大的一笔」走 `list_recent(order="largest", since=…, until=…)` 答得上来;七轮缓存命中 94.3%→91.4%、前缀零重建。挂账销两条:`completion=` 的推理 token 口径是 deepseek 特有的(mimo 实测 `reasoning_tokens=0`)、第 5 项已答得上来;`read_skill` 到达率仍挂着(这轮七次对话零调用),它是路由层面的问题,和记账依从率是两回事

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
| M3 记忆中间层 + 陪伴 | L0 预算截断、话头、检索双路、压缩 v1(话题索引,无状态卡)、夜间归拢、人格改写 | ✅ 完成 |
| M4 第一个领域 bundle | 财务(对话侧):记账、查询、月度复盘、账本与流水的边界 | 🔄 进行中 |
| M5 上手机 | 渠道定型、IM 适配器、用户主渠道路由(服务仍在本机) | ⬜ 未开始 |
| M6 数据面 + 扩张 | 短信入账 provider、健康/学习 bundle、沙箱 | ⬜ 未开始 |
| M7 上线 | VPS、Caddy、镜像、备份、ingress 加固、调度器与晨报 | ⬜ 未开始 |

> 2026-08-18 重排(原 M2=财务+上线):记忆中间层先于功能 bundle,理由见 DESIGN.md D13。
> 2026-08-20 再排(原 M4=上线):上线拆成「在家里能定的事」与「纯搬家」,前者提前后者垫底,理由见 DESIGN.md D14。
