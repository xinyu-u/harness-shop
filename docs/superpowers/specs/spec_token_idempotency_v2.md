# 实施 Spec（v2）：草稿订单的「按对话轮次」令牌幂等

> 给 Claude Code 的任务说明。请**从里往外、按顺序**实施，每改完一层用该层的测试自检。
> **先读完「不可违反的约束」再动手。**
>
> **本版与 v1 的唯一实质差异**：令牌从「后端 → 工具」的传递方式，由 v1 的「共享可变 dict
> （`token_holder`）挂在被缓存的 engine 上」改为 **`contextvars.ContextVar`（per-task 隔离）**。
> 原因见 §3「为什么不用共享 dict」——v1 的写法会在「同一用户并发两轮对话」时令牌串台，
> 重新引入 `b461d35`（store 加 RLock 串行化）刚消灭的那一类「共享可变状态跨执行流串扰」。
> store 层改动与 v1 完全一致，照抄即可。

---

## 1. 目标

`place_order`（路径B：`create_draft_order`）当前**没有创建侧幂等**。当模型在同一个
assistant turn 里重复发出 `place_order`（LLM 偶发，单条回复里塞多个 tool_use），会建出
**多张 pending 草稿、各自锁一份库存**，产生「幽灵预占」，并使重复草稿的资产安全悄悄依赖
`server.py` 里「只取最后一张 draft_id」（`_extract_draft_id`，那句注释「多次下单取最后一张草稿」
就是现状的自白），而非后端不变量。

本次引入**令牌幂等**：后端在「一轮对话」粒度生成一次性令牌，透传到 `create_draft_order`，
**同一令牌只建一张草稿、只锁一份货**。令牌对模型不可见。

---

## 2. 不可违反的约束（GUARDRAILS — 违反即任务失败）

1. **绝不删除、绝不修改 `confirm_draft_order` 里基于订单状态的幂等**：
   ```python
   if order["status"] == "confirmed":
       return order, None   # 幂等：已确认过，不重复扣
   ```
   这是「资产侧」幂等（同一张草稿别扣两次钱），与本次新增的「创建侧」令牌幂等**正交、
   互补、不可互相替代**。本次任务**不允许**以任何理由触碰它（两处：`MemoryStore` 约 217 行、
   `SqliteStore` 约 623 行）。

2. **令牌绝不能由大模型生成，也绝不能进入工具的 `input_model`（即模型可见的入参）。**
   令牌由**后端代码**用 `uuid.uuid4().hex` 生成。模型只负责填 `product_id/size/qty`。
   原因：模型输出不保证唯一、更不保证「重复那次与首次完全一致」，拿它当幂等键会失效。

3. **令牌粒度 = 一轮对话（一次 `submit_message`）。** 同一轮内 `place_order` 调几次都用
   同一个令牌 → 收敛成一张草稿。下一轮是新令牌（属预期，两轮是两次表达，不在本次去重范围）。

4. **不改动 `PlaceOrderInput` 的字段**（不加 token 字段）。令牌走「后端 → 工具实例」的
   工程侧暗线，不走「模型 → 工具入参」。

5. **令牌的传递通道必须 per-task 隔离，不得是「跨请求共享的可变对象」。**
   `server.py` 按 `(user_id, role)` **缓存复用唯一一个 `QueryEngine`**（`sessions` dict）。
   同一用户并发两个 `/chat/stream` 命中同一个 engine。FastAPI 是 asyncio，两个协程会在每个
   `await` 处交错。**任何挂在 engine 上的共享可变槽都会令牌串台**（v1 的 `token_holder` 即此坑）。
   因此本版用 `ContextVar`：每个请求是独立 Task，Task 创建时复制上下文，`.set()` 互不影响。

6. **两套 Store 实现（`MemoryStore` 和 `SqliteStore`）行为必须一致**，因为测试主要用
   `MemoryStore`，主路径跑 `SqliteStore`。两边都要改。

---

## 3. 设计决策（背景，供理解，不要在代码里重新发明）

- **为什么用令牌，不用业务字段**：`(user, product, size, qty)` 这类业务键会误杀「用户真的
  想买两次同款」的合法订单；令牌代表「同一次处理」，天然框住机械重复，不碰业务语义。
- **为什么后端生成**：见约束 2。
- **为什么是「一轮对话」粒度**：「每次工具调用一个令牌」起不到去重作用（双发=两令牌=两草稿）；
  「跨轮归并意图」是产品语义问题，本次不做。一轮一令牌恰好堵住「模型一个 turn 双发」这个
  确定该堵的机械重复。
- **为什么不用共享 dict（v1 → v2 的关键修正）**：v1 把 `{"token": ...}` 挂在 engine 上，
  `submit_message` 写它、工具闭包读它。但 engine 被 `sessions` 按用户缓存复用，且 FastAPI
  并发请求是同一事件循环上的多个协程：

  ```
  turn A:  token_holder["token"] = tokA;  await stream_message ...
  turn B:    token_holder["token"] = tokB;  await ...
  turn A:  恢复 → place_order 读 token_holder → 拿到 tokB   ← 串台
  ```

  `ContextVar` 没有这个问题：每个 HTTP 请求由 uvicorn 调度成独立 asyncio Task，Task 创建时
  `copy_context()` 拿到独立快照，`.set()` 只改本 Task 的副本。同 engine、不同 Task → 令牌天然隔离。
  附带好处：不用再从 `server.py` 伸手改 engine 私有属性、`build_tools` / `get_engine` 签名都不用动，
  改动面比 v1 更小。
  （注：engine 的 `self._messages` 在「同用户并发两轮」下也有串扰，但那是**既有**问题、不在本次范围；
  本版只保证令牌这条新通道不踩同一个坑。）

---

## 4. 改动清单（按此顺序：store → context → tool → engine → server → 测试 → README）

### 4.1 `business/store.py` — `SqliteStore`（与 v1 相同）

**(a) 建表层**：在 `_init_db` 里 `orders` 建表后，补加 `request_token` 列与唯一索引
（沿用现有 `request_id` 那段的 ALTER + 唯一索引写法）：
```python
try:
    self._conn.execute("ALTER TABLE orders ADD COLUMN request_token TEXT")
except sqlite3.OperationalError:
    pass   # 列已存在
self._conn.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_request_token "
    "ON orders(request_token)"
)
```

**(b) `create_draft_order`**：新增 `request_token=None` 参数；在「原子锁库存」之前插入
「先清过期 + 令牌预检复用」；INSERT 带上 `request_token`；并发兜底回查赢家。
```python
def create_draft_order(self, product_id, size, qty, user_id,
                       ttl_seconds=900, request_token=None):
    with self._lock:
        self.release_expired_orders()   # 先清过期，别让过期单挡住合法新单

        # —— 令牌幂等：同令牌已建过草稿 → 复用，不再锁第二份 ——
        if request_token is not None:
            row = self._conn.execute(
                "SELECT id, product_id, size, qty, user_id, status, expires_at "
                "FROM orders WHERE request_token = ?",
                (request_token,),
            ).fetchone()
            if row is not None:
                return {
                    "id": row[0], "product_id": row[1], "size": row[2],
                    "qty": row[3], "user_id": row[4], "status": row[5],
                    "expires_at": row[6],
                }

        try:
            # ① 原子预占（原逻辑不变）
            cur = self._conn.execute(
                "UPDATE inventory SET locked = locked + ? "
                "WHERE product_id = ? AND size = ? AND qty - locked >= ?",
                (qty, product_id, size, qty),
            )
            if cur.rowcount == 0:
                raise ValueError(f"库存不足: {product_id} {size}")

            # ② 建 pending 草稿（多带 request_token 列）
            expires_at = time.time() + ttl_seconds
            cur2 = self._conn.execute(
                "INSERT INTO orders "
                "(product_id, size, qty, user_id, status, expires_at, request_token) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (product_id, size, qty, user_id, "pending", expires_at, request_token),
            )
            new_id = cur2.lastrowid
            self._conn.commit()
            return {
                "id": new_id, "product_id": product_id, "size": size,
                "qty": qty, "user_id": user_id, "status": "pending",
                "expires_at": expires_at,
            }
        except sqlite3.IntegrityError:
            # 并发兜底：两次同令牌几乎同时到（当前 RLock 已串行化，撞不上；
            # 为将来连接池保留）。撤销自己刚锁的预占，回查赢家草稿返回。
            self._conn.rollback()
            if request_token is not None:
                row = self._conn.execute(
                    "SELECT id FROM orders WHERE request_token = ?", (request_token,)
                ).fetchone()
                if row is not None:
                    return self.get_order(row[0])
            raise
        except ValueError:
            self._conn.rollback()
            raise
        except Exception:
            self._conn.rollback()
            raise
```
> **行为变更（非纯增量，须知会评审）**：起手新增的 `self.release_expired_orders()` 是现有
> `create_draft_order` **没有**的——它对齐 `confirm` / `check_stock` 已有的「惰性释放」习惯，
> 顺手保证新单不被过期单挡住。沿用 v1 原意保留。
>
> 注意：`get_order` 当前不返回 `expires_at`，并发兜底分支返回的 dict 缺该键是可接受的
> （调用方只用到 `id`）。本次不强求统一。

### 4.2 `business/store.py` — `MemoryStore`（与 v1 相同）

`create_draft_order` 同样加 `request_token=None`，行为对齐：
```python
def create_draft_order(self, product_id, size, qty, user_id,
                       ttl_seconds=900, request_token=None):
    self.release_expired_orders()
    if request_token is not None:
        for o in self._orders:
            if o.get("request_token") == request_token:
                return o   # 同令牌复用
    key = (product_id, size)
    available = self._inventory.get(key, 0) - self._locked.get(key, 0)
    if available < qty:
        raise ValueError(f"库存不足: {product_id} {size}")
    self._locked[key] = self._locked.get(key, 0) + qty
    order = {
        "id": len(self._orders) + 1,
        "product_id": product_id, "size": size, "qty": qty,
        "user_id": user_id, "status": "pending",
        "expires_at": time.time() + ttl_seconds,
        "request_id": None,
        "request_token": request_token,
    }
    self._orders.append(order)
    return order
```

### 4.3 `core/tools.py` — 定义令牌的 per-task 通道（v2 新增，替代 v1 的 token_holder）

在文件顶部 import 区加 `contextvars`，并定义模块级 `ContextVar`。放这里的理由：
`core/tools.py` 已被 `core/engine.py`（写令牌）与 `business/cs_tools.py`（读令牌）**双向 import**，
是唯一无 import 环的公共落点。
```python
from contextvars import ContextVar

# 「当前这一轮对话的令牌」。由 engine.submit_message 在每轮开头 .set()，
# 由 place_order 工具在 execute 时 .get()。ContextVar 是 per-task 的：
# 每个 HTTP 请求是独立 asyncio Task，Task 创建时复制上下文，.set() 互不串扰。
# 未设置时取默认 None → 工具拿到 None → 不去重（CLI / eval 等不传令牌的入口自然降级）。
request_token_var: ContextVar[str | None] = ContextVar("request_token", default=None)
```

### 4.4 `business/cs_tools.py` — `PlaceOrderTool`

`PlaceOrderInput` **不动**；`PlaceOrderTool.__init__` **不动**（仍是 `store, user_id`）；
`build_tools` **不动**（不再有 `token_holder` 参数）。只在 `execute` 里读 `ContextVar`：
```python
from core.tools import BaseTool, ToolResult, request_token_var   # 顶部 import 加 request_token_var

class PlaceOrderTool(BaseTool):
    ...
    async def execute(self, arguments: PlaceOrderInput) -> ToolResult:
        # 令牌走工程侧暗线（ContextVar），模型入参里没有它（约束 4）。
        token = request_token_var.get()
        try:
            draft = self._store.create_draft_order(
                arguments.product_id, arguments.size, arguments.qty,
                user_id=self._user_id, request_token=token,
            )
        except ValueError as e:
            return ToolResult(output=f"下单失败：{e}", is_error=True)
        except Exception as e:
            return ToolResult(output=f"下单失败：{e}", is_error=True)
        return ToolResult(output=(
            f"已生成待确认订单 #{draft['id']}："
            f"{arguments.product_id} {arguments.size}码 ×{arguments.qty}，"
            f"请在15分钟内确认。订单号：{draft['id']}"
        ))
```
> 对照 v1：**删去** v1 的 `token_provider` 入参、`build_tools` 的 `token_holder` 入参与闭包。
> 工具直接读 `ContextVar`，少一层管道。

### 4.5 `core/engine.py` — `QueryEngine.submit_message`

`submit_message` 接收 `request_token`，在每轮开头写入 `ContextVar`。**不再需要** `self._token_holder`。
```python
from core.tools import BaseTool, request_token_var   # 顶部 import 加 request_token_var

class QueryEngine:
    async def submit_message(self, prompt: str, request_token: str | None = None):
        # 本轮令牌写入 per-task 上下文，供 place_order 工具 .get()。
        # submit_message 是 async generator，在调用方的同一个 Task 内被迭代，
        # 所以这里 set 的值对后续 run_query → tool.execute 可见；并发的另一轮在它自己的
        # Task 上下文里，互不影响（约束 5）。
        request_token_var.set(request_token)
        ...   # 其余（拼 system_prompt、append 消息、run_query）一字不改
```
> 不需要 reset：server 每请求一个 Task，Task 结束上下文即销毁；CLI（`main.py`）在同一 Task 里
> 顺序多轮，每轮开头都会覆写，不会读到上一轮的残值。

### 4.6 `server.py` — 接线 + 每轮生成令牌

**`get_engine` 不动**（v1 需要在这里塞 `token_holder` 并伸手改 `engine._token_holder`，v2 完全不需要）。
只在两个聊天入口每轮生成令牌并传入：
```python
import uuid   # 顶部 import 区

# /chat/stream 内，engine = get_engine(...) 之后、进入 generate() 前（或 generate 内首行）：
request_token = uuid.uuid4().hex
...
async for event in engine.submit_message(req.message, request_token=request_token):
    ...

# /chat（非流式）同样：
request_token = uuid.uuid4().hex
async for event in engine.submit_message(req.message, request_token=request_token):
    ...
```
> `request_token` 在 endpoint 协程（= 该请求的 Task）里生成并传入，`ContextVar.set()` 发生在
> 这个 Task 的上下文，天然与别的请求隔离。

---

## 5. 其它入口的降级（CLI / eval —— 不传令牌即不去重，可接受）

以下入口仍调旧签名、不传令牌，因参数有默认值不会报错，行为退化成「现状（无创建侧去重）」：

- `main.py`（CLI）：`engine.submit_message(prompt)` → 令牌 None → 不去重。CLI 单线程、无并发双发，可接受。
- `eval/harness.py`：`engine.submit_message(prompt)` → 同上，评测harness 不依赖令牌去重。

**不需要改它们**；此处显式登记，避免「以为四层就是全部」的错觉。若日后要让 eval 覆盖令牌路径，
再单独给 harness 加一个可选 `request_token` 透传，不混入本次。

---

## 6. 旧幂等的处置（与 v1 相同）

- **路径A `create_order` 的 `request_id` 幂等**：保持现状。本次不删路径A。
- `orders.request_id` 列与 `idx_orders_request_id` 索引：**保留**，服务路径A，与新 `request_token` 互不干扰。
- **若此前曾加过 `(user, product, size[, qty])` 业务字段去重**：删除它，由令牌幂等取代。（若从未落地，跳过。）

---

## 7. 测试（新增；放 `tests/test_draft_order.py`，并发隔离测试可放同文件或 `tests/test_token_idempotency.py`）

### 7.1 store 层（与 v1 相同，证明「同令牌一张草稿」）
```python
def test_same_token_dedupes_draft():
    store = MemoryStore()                      # airmax 42 初始库存 5
    d1 = store.create_draft_order("airmax", "42", 1, "alice", request_token="tok-1")
    d2 = store.create_draft_order("airmax", "42", 1, "alice", request_token="tok-1")
    assert d1["id"] == d2["id"]                # 同令牌 → 同一张草稿
    assert store.check_stock("airmax", "42") == 4   # 只锁 1 份，非 2 份

def test_different_token_creates_new_draft():
    store = MemoryStore()
    d1 = store.create_draft_order("airmax", "42", 1, "alice", request_token="tok-1")
    d2 = store.create_draft_order("airmax", "42", 1, "alice", request_token="tok-2")
    assert d1["id"] != d2["id"]                # 不同轮/不同令牌 → 两次合法意图

def test_no_token_still_works():
    store = MemoryStore()
    d = store.create_draft_order("airmax", "42", 1, "alice")   # 不传令牌不报错
    assert d["status"] == "pending"
```
- 理想：对 `SqliteStore`（临时 db）复制同样三条断言，确认**主路径**真的修好。

### 7.2 端到端：同一 turn 双发收敛成一张（证明 engine→工具→store 整条管线接对了）
```python
import asyncio
from core.client import FakeClient
from core.engine import QueryEngine
from core.messages import ConversationMessage, ToolUseBlock, TextBlock
from business.store import MemoryStore
from business.cs_tools import build_tools

async def test_double_fire_in_one_turn_dedupes():
    store = MemoryStore()
    # 一条 assistant 回复里塞两个 place_order（模型同 turn 双发）
    scripted = [
        ConversationMessage(role="assistant", content=[
            ToolUseBlock(name="place_order", input={"product_id": "airmax", "size": "42", "qty": 1}),
            ToolUseBlock(name="place_order", input={"product_id": "airmax", "size": "42", "qty": 1}),
        ]),
        ConversationMessage(role="assistant", content=[TextBlock(text="已为你下单")]),
    ]
    engine = QueryEngine(FakeClient(scripted=scripted), build_tools(store, "alice"), user_id="alice")
    _ = [e async for e in engine.submit_message("买双 airmax 42", request_token="turn-1")]

    pendings = [o for o in store._orders if o["status"] == "pending"]
    assert len(pendings) == 1, f"同 turn 双发应只建一张草稿，实际 {len(pendings)}"
    assert store.check_stock("airmax", "42") == 4, "应只锁 1 份"
```

### 7.3 并发隔离回归（v2 专属——这条若用 v1 的共享 dict 会失败）
```python
import asyncio
from core.tools import request_token_var

async def test_token_var_isolated_across_concurrent_turns():
    seen = {}
    async def turn(name, token):
        request_token_var.set(token)
        await asyncio.sleep(0)        # 让出控制权，逼两个协程在此交错
        seen[name] = request_token_var.get()
    # gather 把每个协程包成独立 Task，各自复制上下文 → .set 互不串扰
    await asyncio.gather(turn("A", "tokA"), turn("B", "tokB"))
    assert seen == {"A": "tokA", "B": "tokB"}, f"令牌串台了：{seen}"
```
> 这条是 v2 的核心理由的可执行证据：把它换成「一个共享 dict 槽」写法，断言必挂。

- **回归**：跑完整 `tests/`，确认 `test_confirm_flow.py` 全绿——尤其「重复确认幂等」用例
  必须仍通过（验证 confirm 幂等未被破坏，即约束 1）。

---

## 8. README 更新（与 v1 相同）

在「下单确认」或「数据」一节补一句：
> 草稿创建对「同一轮对话的令牌」幂等：令牌由后端生成、模型不可见，同令牌只建一张草稿、
> 只锁一份预占。资产级幂等仍在 confirm 环节按订单状态兜底——**创建侧（令牌）与资产侧
> （订单状态）是两道正交防线，互补不可替代**。

---

## 9. 验收命令

```bash
python -m pytest tests/ -v          # 全绿；新增 store 三条 + 端到端双发 + 并发隔离 + confirm_flow 回归
python tests/smoke_auth.py          # 不回归
python tests/smoke_role_gate.py     # 不回归
```

**完成定义（DoD）**：
1. 同一轮 `place_order` 双发只产生一张草稿、只锁一份库存（§7.2 证明）。
2. 同 engine 上并发两轮、不同令牌互不串台（§7.3 证明，约束 5）。
3. `confirm_draft_order` 的状态幂等**逐字未改**（约束 1）。
4. 模型入参未出现令牌字段（约束 4）；令牌由后端 `uuid4` 生成（约束 2）；通道是 per-task `ContextVar`（约束 5）。
5. `MemoryStore` 与 `SqliteStore` 行为一致；全部测试 + 两个 smoke 通过。

---

## 10. 不在本次范围（别顺手做）

- 跨轮的「意图归并」（粒度三）——产品语义问题，需单独决策。
- engine `self._messages` 在「同用户并发两轮」下的串扰——**既有**问题，独立任务，本次只保证令牌通道不踩同坑。
- 删除路径A / 清理 `request_id` 列——独立任务。
- 连接池 / WAL——`request_token` 唯一索引已为其预留并发兜底，但切换连接池本身不在本次。
- 前端按钮置灰防抖——体验层改动，单独做。
- 给 `main.py` / `eval/harness.py` 补令牌透传——降级可接受，要做单列（见 §5）。
