# 高并发评估设计（eval/）

日期：2026-06-23
状态：已批准设计，待实现

## 目标

给现有 `eval/` 套件补一个新维度：**并发安全**。验证业务在高并发下不破防——
重点是数据层的并发不变量（不超卖、幂等、状态机竞态、守恒、无丢失更新），
外加一层 agent 端到端的负载冒烟。

拆成两个独立脚本，对应两层：

| 脚本 | 性质 | 测什么 | 阈值 | 烧 API |
|---|---|---|---|---|
| `eval/eval_concurrency.py` | 确定性 | 真线程并发锤 `SqliteStore`，断言并发不变量 | 1.0 | 否 |
| `eval/eval_concurrency_agent.py` | 概率性 | K 个真实 agent 对话共享同一 store 抢库存 → 不超卖 | 1.0（不超卖）| 是 |

## 关键设计决定

### 1. 复现 server.py 的并发模型：单连接共享 SqliteStore

`server.py:46` 用一个模块级 `store = SqliteStore()`，被所有 FastAPI 请求**跨线程共享**
（sync endpoint 跑在 threadpool）。这就是真实部署形态，也是要暴露的风险面——真正的隐患不在
单条"原子 `UPDATE ... WHERE qty>=?`"（单看是对的），而在一个连接跨线程时未提交事务与
其它线程的 commit/rollback 互相串扰（共享同一事务上下文）。

所以并发套件**默认用单个共享连接**，由 `ThreadPoolExecutor` 真线程并发调用。
不做"每线程独立连接"对照档（那测的是 SQLite 文件锁，偏离部署现状）。

### 2. 只测前端真能制造的并发，不测保留代码

实际部署的写链：

```
place_order 工具 → create_draft_order(无 request_id)
              → server 抠 draft_id → /orders/{id}/confirm → confirm_draft_order
              → /orders/{id}/cancel → cancel_order
restock（商家工具）
```

**路径A（`create_order` + `request_id` 唯一索引幂等）完全不纳入并发套件**——
`store.py:31` / `README.md:191` 明确它"没人在 web 流里调，有意保留作对照"。
测它的并发 = 测部署里的死代码。它的幂等若要保，放普通单测，不归这里。

注意区分两种"幂等"：`create_order` 的 request_id 幂等（路径A，砍）vs
`confirm_draft_order` 的 `status=='confirmed'` 幂等（路径B，前端双击/重发确认真能触发，留）。
`frontend/index.html` 的 `orderState` 禁用按钮只是客户端防抖，服务端不能依赖它。

### 3. 确定性，阈值 1.0

数据层并发不变量是代码机制保证的，挂了 = 真 bug，归 README 的"确定性"桶
（断言式、阈值 1.0、求机制覆盖完整）。agent 层负载冒烟是概率性，但"不超卖"这一条仍按 1.0。

### 4. 复用现有 harness，侵入最小

- **复用** `harness.py` 的 `_fresh_sqlite()`、`Outcome`、`summarize`、`print_report`——
  报表/退出码风格与现有四个套件一致。
- **不复用** `run_suite`（它是 "agent trace + judge" 形态，确定性并发数据层不需要 agent）。
  `eval_concurrency.py` 自带一个小 runner：每个场景是 `scenario() -> Outcome`，跑完汇总。
- agent 层需要能**注入共享 store**：给 `run_case` 加可选 `store=` 参数——传入则共享该 store、
  且 `trace.cleanup()` 不删它（由调用方统一关闭）；不传则 `_fresh_sqlite()`，行为不变。
  这是唯一的 harness 侵入式改动。

## eval_concurrency.py：不变量清单

每条 = 一个并发场景 + 一个确定性断言。每个场景独立用 `_fresh_sqlite()` 起一个共享连接。

| # | 场景 | 断言（PASS 条件，见下方"判定口径"） |
|---|---|---|
| 1 | 库存5，20 线程并发 `create_draft_order(qty=1)` | **API/DB 一致**：返回成功的调用数 == DB 里 pending 订单行数 == `locked` 总量，且该值 `<= 5`；qty/locked 永不为负。不写死"恰好 5" |
| 2 | 一张 pending 草稿，N 线程并发 `confirm_draft_order` | 恰好一个调用拿到"刚转成 confirmed"的返回、其余幂等返回同一单；qty 只扣一次、locked 只释放一次、终态 `status=='confirmed'` |
| 3 | 同一草稿，`confirm` 与 `cancel` 并发竞态 | **跨层不撒谎**：`confirm 返回成功` 与 `cancel 返回 True` 至多一个为真；且赢家的返回值与 DB 终态一致（confirmed → qty 扣 1/locked 释放；cancelled → locked 释放/qty 不动）。绝不"两个调用方都被告知成功" |
| 4 | 混合并发 create_draft / confirm / cancel | 全程恒成立：`locked>=0`、`qty>=0`、`available==qty-locked`；无订单被双计 |
| 5 | N 线程并发 `restock(add_qty=1)` | 末态 `qty == 起始 + N`（无丢失更新） |

### 判定口径

**核心原则：断言"安全不变量 + API/DB 一致性"，而非"完美计数"。** 单连接跨线程事务串扰下，
失败是混沌的——可能超卖、可能因别的线程 `rollback()` 抹掉本线程未提交的写而"丢失更新"
（Python 层以为成功、DB 里没那行）、也可能大面积 `sqlite3.OperationalError: database is locked`。
写死"恰好 5 成功"既脆弱又会用"期望5实得2"掩盖真正病灶。所以：

- **成功口径**：调用"返回成功"才算成功（create_draft 不抛即成功；confirm 返回 `(order, None)`
  且该单是本次刚转 confirmed；cancel 返回 `True`）。**绝不 `except`-吞 `OperationalError`**——
  它是头等症状，单列一个 outcome 桶。
- **诊断分布（必打印，不参与判定）**：每场景 tally 各结果桶——
  成功 / `ValueError`(库存不足) / `OperationalError`(database is locked) / 其它异常。
  正确实现下场景1 应是 5/15/0/0；但 PASS 由不变量决定，桶用来在 FAIL 时暴露病灶。
- **不变量从 store 直接查终态**（`check_stock` / 直接 SQL 读 `inventory` `orders`），
  与"返回成功的调用数"对账——对不上即 FAIL（这正是抓"丢失更新/谎报成功"的关键）。
- 任一断言不成立 → `Outcome.FAIL`；全成立 → `Outcome.PASS`。并发套件无 N/A。
- 每条不变量内部多跑几轮（并发调度有随机性，单跑可能恰好不撞车）——
  默认每场景重复 R 轮（如 R=5），任一轮 FAIL 即该条 FAIL。

## eval_concurrency_agent.py：负载冒烟

**并发必须用真线程，不能用 `asyncio.gather`。** agent 对话是 async 的，但 async 只来自 LLM
网络调用——工具体里的 `SqliteStore` 是**同步阻塞 `sqlite3`**，不含 `await`、不让出事件循环。
`asyncio.gather` 下协程只在 `await` 处交错，一次工具调用会跑到底不让出 → DB 操作被事件循环
**串行化**，恰好把确定性套件想暴露的事务串扰物理隔离掉，100% 假性通过。而且这也不符
`server.py`：FastAPI 的 sync `def` 端点跑在**线程池**，是真线程打共享连接。所以冒烟要复现这个。

- K 个真实 agent 对话由 `ThreadPoolExecutor` 驱动，**一个线程一个对话**，每线程内
  `asyncio.run(run_case(..., store=共享store))` 起自己的事件循环，**各自新建一个 `OpenAIClient`**
  （async client 不能跨事件循环共享，所以这里不复用单 client）；K 个对话**共享同一个 `SqliteStore`**。
  这复现 server.py 的"线程池 + 单连接共享"模型。
- 库存设为 N（如 3），K > N（如 10），各自 prompt 为"我要买最后那双 airmax 42"之类。
- 断言：经 agent 全链路返回成功建成的 pending 草稿数与 DB 实际 pending 行数一致、且 ≤ N
  （不超卖、不谎报）；无未捕获异常（`OperationalError` 单列上报）；`available` 不为负。
- 阈值：不超卖 1.0。烧 API，归概率桶但这条按硬标准。

## 文件与改动清单

- 新增 `eval/eval_concurrency.py`（确定性，自带 runner）。
- 新增 `eval/eval_concurrency_agent.py`（概率，`ThreadPoolExecutor` 一线程一对话 + 每线程独立 client + 共享 store）。
- 改 `eval/harness.py`：`run_case` 加可选 `store=` 参数（共享时不 cleanup db）。
- 改 `eval/README.md`：维度表加两行、跑法加命令、留一条"评估发现"占位。

## 跑法（预期）

```bash
# 确定性并发套件（不烧 API）
python -m eval.eval_concurrency
python -m eval.eval_concurrency --threshold 1.0

# agent 负载冒烟（烧 API）
python -m eval.eval_concurrency_agent
```

退出码：通过率 ≥ 阈值 → 0，否则 1（接 CI）。

## 预期：可能跑出真 FAIL

单连接跨线程在事务边界上本就不安全（一个 `commit/rollback` 作用于整个连接，会波及其它
线程未提交的写）。所以本套件**很可能在现有 `store.py` 上跑出真实 FAIL**——这是 eval 在干活，
不是 eval 写错。修复（如每请求一连接、或一把序列化写锁/写队列）是**独立的后续工作，不在本 eval 范围**。
本 eval 的交付物是"能稳定复现并诊断问题的探针"，不是修复。

## 不做（YAGNI）

- 不做路径A / request_id 并发幂等（部署死代码）。
- 不做"每线程独立连接"对照档（偏离 server.py 部署形态）。
- 不做吞吐/延迟性能基准（这是正确性评估，不是压测）。
