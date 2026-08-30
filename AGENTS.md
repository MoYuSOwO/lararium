# AGENTS.md

给在本仓库工作的编码 agent 的导航。**本文件会被每个会话加载,所以只放导航和高频命令。**
新规则写进 `CONVENTIONS.md`,新步骤写进 `PLAN.md`,不要往这里堆——它膨胀一行,
以后每一轮对话都要为它付钱。

## 项目

Lararium:跑在用户自己服务器上的个人生活助手,通过一个 IM 对话框管理账单、运动、
学习、待办。架构是**单 agent + plugin bundle**:一个主控(Steward)持有全部智能,
各生活领域是独立的 MCP bundle,只提供工具和数据,不含 LLM。

**当前状态**:M1–M4 已完成(骨架 / 前后端分离 / 记忆中间层 / 第一个领域 bundle),
M5(上手机)收尾中——微信通道、媒体入站、读图都已交付。进度见 `REVIEW.md` 的验收记录、
待办见 `PLAN.md` 的 M5 段。

## 按需读,别全读

文档是分层的,和项目自己的 skill 路由同一个思路——用到哪层读哪层:

| 什么时候 | 读什么 |
|---|---|
| 写任何代码前 | `CONVENTIONS.md` 的 **S(结构)** 和 **F(函数与数据)** 两组 |
| 开始某个任务前 | `PLAN.md` 的「全局约束」「工程纪律」+ 该任务全文 |
| 想知道"为什么这么设计" | `DESIGN.md` 对应章节(计划里每个任务都标了) |
| 交付、验收 | `REVIEW.md` |

`DESIGN.md` 是参考手册,不要通读——它比你这次需要的多得多。

**`PLAN.md`(5700 行)和 `REVIEW.md`(8100 行)绝对不要整份读进上下文。**
它们是按里程碑追加的档案,九成的内容和你手上这个任务无关,读全份就是把上下文烧在
历史上——而这个项目最贵的东西就是上下文。**按章节取**:

```bash
grep -n "^## \|^# M" PLAN.md          # 先看目录,找到你要的那节在哪
awk '/^## M4-3/,/^## M4-4/' PLAN.md   # 只取那一节(两个标题之间)
awk '/^# M4 /,/^# M5 /' REVIEW.md     # 整个里程碑的验收记录
grep -n "假绿\|P1-1" REVIEW.md        # 找某个具体教训
```

注意 `## ` 标题只有**已展开的任务**才有;还没轮到的(M5-3 之后那几条)是里程碑段落里的
列表项,`grep -n "M5-3"` 找行号再按行取。

`REVIEW.md` 尤其如此:它**是历史,不是规格**。查"当初为什么这么定"才去翻,
按主题 grep,别顺序读。

## 命令

门禁全跑(提交前必过,pre-commit 钩子也会自动跑这套):

```bash
uv run ruff check src bundles tests && uv run ruff format --check src bundles tests && uv run mypy && uv run lint-imports && uv run pytest -q
```

单个测试文件:

```bash
uv run pytest tests/steward/test_inbox.py -v
```

首次环境准备:

```bash
uv sync && uv run pre-commit install
```

语义检索的权重要转一次(一次性,离线;不转的话 `recall_similar` 一直回"暂不可用",
词法检索照常):

```bash
uv run python scripts/build_embedding_weights.py
```

跑起来(M2 起是双进程:常驻服务 + 普通客户端,都需要先照 `.env.example` 配好 `.env`):

终端 A——起服务(worker 在同一进程,lifespan 里起 task):

```bash
set -a && source .env && set +a \
  && LARARIUM_TOKENS=cli:tok-dev LARARIUM_INGEST_TOKENS= \
  && uv run python -m lararium.gateway.server
```

终端 B——起 CLI 客户端(纯 HTTP,零特殊地位):

```bash
export LARARIUM_SERVER_URL=http://127.0.0.1:8420 LARARIUM_CLIENT_TOKEN=tok-dev
uv run python -m lararium.gateway.cli
```

终端 C(可选)——微信适配器(M5-3,独立进程;第一次跑会打一条扫码链接):

```bash
export LARARIUM_SERVER_URL=http://127.0.0.1:8420 LARARIUM_CLIENT_TOKEN=tok-wx
uv run python -m lararium.gateway.wechat
```

服务端那边要给它一个**渠道叫 wechat** 的控制端 token(`LARARIUM_TOKENS=wechat:tok-wx`),
再把 `LARARIUM_PUSH_CHANNEL=wechat`,主动推送才落到微信而不是 cli。

微信里**以 `/` 开头的消息走命令端点**(`/pending` `/approve <id>` …),和 CLI 同一套分派;
别的当普通对话。ClawBot 没有按钮,审批就是打一行命令。

注:`LARARIUM_TOKENS` 是控制端(全权:消息/出件箱/命令/健康),`LARARIUM_INGEST_TOKENS`
是数据面(只准入站;命令端点是门控开关,数据面不许碰)。冒烟(真实 API)见 REVIEW M2-6。

## 不可协商

三条设计命根子,违反了整个架构就塌了:

1. **前缀区(system prompt)必须逐字节稳定**——时间戳、随机数、每轮变化的东西一律不许进。
   缓存命中是设计约束,不是优化项。
2. **账本只有一条写入路径**:`Gate.settle()`。别处写账本等于给提示注入开后门。
3. **进过模型上下文的一切必须落起居注**,且落的是模型实收的那一份,不是事后重拼的。

外加一条工程纪律:**禁止 `git commit --no-verify`。** 门禁报错就修;
确实需要例外,按 `CONVENTIONS.md` G4 用最小范围抑制并写明理由。

## 工作方式

- **一次只做一个任务**,做完停下等验收。任务之间有契约依赖,连着做会让签名走偏后全线返工。
- 一个任务的「做完」= 步骤走完 + 门禁过 + commit + 在 `REVIEW.md` 登记待验收;
  **验收通过后立刻往 `CHANGELOG.md` 的当前里程碑追加一行**,别攒着。
- 任务内步骤按顺序走,**包括「运行测试确认失败」那一步**——那是在验证测试真的测到了东西,
  不是走过场。
- **计划里的代码可能有错**(核对过类型和逻辑,但大部分没实跑过)。遇到错误就修,
  然后在回报里说明改了什么、为什么。不要照抄跑不通的代码,也不要顺手重新设计。
  偏离计划没问题,不说才是问题。
- 不要自己改 `DESIGN.md` / `PLAN.md` / `CONVENTIONS.md`。有异议在回报里提。

## 目录

```
src/lararium/
  config.py envelope.py db.py        基础设施(db.py:连接一律从这里建,见下)
  persona.py                          前缀第 1 层:人设 + 纪律,以及前缀指纹
  steward/                            主控:唯一持有智能的地方
    ports.py                          对 bundle 的抽象(守 import 边界)
    inbox.py outbox.py journal.py     收件箱、出件箱、起居注(Steward 独占存储)
    registry.py assembler.py tools.py 注册表、上下文组装、内置工具
    threads.py compact.py sweep.py    话头、压缩、夜间归拢
    embeddings.py vision.py           本地 embedding、图片进模型那一层
    model.py                          第三方库隔离盒(类型宽松档)
    loop.py worker.py                 一轮的编排、常驻 worker
  gateway/
    server.py                         **组装根**:唯一能 import bundles 的地方
    commands.py                       斜杠命令分派(审批权在这里,只此一份)
    cli.py ilink.py wechat.py         纯 HTTP 客户端,不许 import steward/bundles
bundles/memory/                       Memory 也是一个 bundle,没有特例
  ledger.py                           账本 + 快照表(唯一写入路径 Gate.settle)
  gate.py                             门控状态机
  server.py                           FastMCP server(类型宽松档)
bundles/finance/                      第一个生活领域:记账与消费分析
tests/test_architecture.py            项目不变量门禁
```

**数据库连接一律走 `db.connect()` / `db.open_connection()`,不许自己 `sqlite3.connect`。**
同步工具函数跑在框架给的线程池里,一条 assistant 消息里的多个工具调用是**并发**的;
裸连接在那种并发下会烂掉游标,症状是三种互不相干、指不到任何地方的异常(M5-8)。

边界由 `.importlinter` 强制:`lararium.steward` 不许依赖 `bundles`,bundle 之间不许互相依赖。
需要 Memory 的能力就走 `ports.py` 的 Protocol,由 `cli.py` 接线。
