# AGENTS.md

给在本仓库工作的编码 agent 的导航。**本文件会被每个会话加载,所以只放导航和高频命令。**
新规则写进 `CONVENTIONS.md`,新步骤写进 `PLAN.md`,不要往这里堆——它膨胀一行,
以后每一轮对话都要为它付钱。

## 项目

Lararium:跑在用户自己服务器上的个人生活助手,通过一个 IM 对话框管理账单、运动、
学习、待办。架构是**单 agent + plugin bundle**:一个主控(Steward)持有全部智能,
各生活领域是独立的 MCP bundle,只提供工具和数据,不含 LLM。

**当前状态**:设计与计划已定稿,正在实现 M1(骨架)。进度见 `REVIEW.md` 的任务验收表。

## 按需读,别全读

文档是分层的,和项目自己的 skill 路由同一个思路——用到哪层读哪层:

| 什么时候 | 读什么 |
|---|---|
| 写任何代码前 | `CONVENTIONS.md` 的 **S(结构)** 和 **F(函数与数据)** 两组 |
| 开始某个任务前 | `PLAN.md` 的「全局约束」「工程纪律」+ 该任务全文 |
| 想知道"为什么这么设计" | `DESIGN.md` 对应章节(计划里每个任务都标了) |
| 交付、验收 | `REVIEW.md` |

`DESIGN.md` 是参考手册,不要通读——它比你这次需要的多得多。

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

跑起来(Task 11 之后可用,需要先照 `.env.example` 配好 `.env`):

```bash
set -a && source .env && set +a && uv run python -m lararium.gateway.cli
```

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
  config.py envelope.py db.py        基础设施
  steward/                            主控:唯一持有智能的地方
    ports.py                          对 bundle 的抽象(守 import 边界)
    inbox.py journal.py               收件箱、起居注(Steward 独占存储)
    registry.py assembler.py tools.py 注册表、上下文组装、内置工具
    model.py                          第三方库隔离盒(类型宽松档)
    loop.py                           一轮的编排
  gateway/cli.py                      组装根:唯一能 import bundles 的地方
bundles/memory/                       Memory 也是一个 bundle,没有特例
  ledger.py                           账本 + 快照表(全系统唯一写文件的模块)
  gate.py                             门控状态机
  server.py                           FastMCP server(类型宽松档)
tests/test_architecture.py            项目不变量门禁
```

边界由 `.importlinter` 强制:`lararium.steward` 不许依赖 `bundles`,bundle 之间不许互相依赖。
需要 Memory 的能力就走 `ports.py` 的 Protocol,由 `cli.py` 接线。
