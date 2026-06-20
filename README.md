# 客服 Agent（自研 harness 引擎）

一个从零实现的电商客服 agent —— 不依赖 LangChain，自己写了一套轻量 harness。

> **harness 是什么**：模型本身只会"输入文字 → 输出文字 / 工具调用"。把它变成一个能查库存、
> 下单、记住用户的 agent，中间那一层工程代码就是 harness —— 负责循环调度、工具调用、
> 上下文管理、权限控制、记忆。本项目的 harness 实现了这六样，约 2000 行，与具体业务无关。

**核心解决一个真实问题：当 agent 能操作业务系统（下单、改价、退库存）时，怎么控制它的权限边界。**
本项目用两层机制回答：

- **工具读写分级**：只读工具（查库存、推荐尺码）agent 自由调；写操作（下单、取消）需确认，防误操作。
- **角色门禁**：敏感工具（改价、上架）按角色隔离 —— 普通用户的模型根本看不到这些工具，
  商家才能调。门禁从 JWT 的 role 一路穿透到工具层。

业务上是个最小可用的店面：买家可搜商品、查库存、推荐尺码、下单、取消、查订单；商家可改价、上架。
界面是单文件 Vue + FastAPI 后端，账号走 bcrypt + JWT，每个用户独立 engine 会话，
记忆按 `user_id` 落到本地 `memory_{uid}.md`。

---

## 架构

```
frontend/index.html          单页 Vue：登录/注册 + 对话 UI，token 存 localStorage
       │  POST /register · /login        →  JWT
       │  POST /chat/stream  Bearer <t>  →  NDJSON 流：status / final / error
       │  POST /chat         Bearer <t>  →  一次性 JSON（curl 调试用，前端不走）
       ▼
server.py                    FastAPI 外壳
                               · 启动时按 .env 种 merchant 账号
                               · /register 永远建 role='user'
                               · /chat 解 JWT 拿 (user_id, role) → 取/建 engine
                               · /chat/stream 同上,把 engine 的事件流推给前端
                               · /orders/{id}/confirm · /orders/{id}/cancel 独立确认/取消订单（不经 agent，校验归属，见"下单确认"）
                             sessions key = (user_id, role)，避免缓存串色
       ▼
core/  ── harness 引擎（通用，与业务无关）
  messages.py   TextBlock / ToolUseBlock / ToolResultBlock / ConversationMessage
  events.py     AssistantTurnComplete / ToolExecutionStarted / ToolExecutionCompleted
  client.py     ModelClient 协议 + OpenAIClient + FakeClient（测试用）
  tools.py      BaseTool 基类（is_write + allowed_roles）+ ToolResult
  compact.py    超阈值时把旧的 tool_result 清成占位符（保留最近 5 条）
  memory.py     读 / 追加 memory_{user_id}.md
  auth.py       bcrypt 哈希 + JWT 签发/校验（jwt_secret 从环境变量读）
  engine.py     RunConfig（含 role）+ run_query（按 role 过滤工具）+ QueryEngine
       ▼
business/ ── 客服业务
  store.py      Store 接口 + MemoryStore + SqliteStore（含 users 表）
  cs_tools.py   9 个工具（见下文），全部只调 Store 接口
       ▼
main.py                      CLI 入口（同样接 QueryEngine，只是输出方式不同）
tests/                       FakeClient 驱动的单元/冒烟测试
```

四条设计原则：

- **内部统一格式 + client 翻译**：harness 里只有 `ConversationMessage / Block`，
  各 provider 的 client 负责翻译成对方格式（`OpenAIClient._to_openai` / `_from_openai`）。
- **工具只调 Store 接口**：换底层存储（字典 → SQLite → …）工具一行不改。
- **工具属性标记 + 策略在循环里**：`is_write=True` 决定要不要 confirm；
  `allowed_roles={"merchant"}` 决定哪些角色能看到/能调。工具本身不知道自己被特殊对待。
- **role 一路穿透**：JWT payload `{sub, role, exp}` → `decode_token` →
  `get_engine(user_id, role)` → `RunConfig.role` → `run_query` 里
  按 role 过滤工具 schema（模型根本看不到自己用不了的工具）+ 执行前再兜底拦一次。

---

## 一次 `/chat/stream` 发生了什么

1. **server.py** 从 `Authorization: Bearer <token>` 解 JWT 拿 `(user_id, role)`，
   按 `(user_id, role)` 找/建 `QueryEngine`，调 `submit_message(text)`。
2. **engine** 把当前用户的 `memory_{uid}.md` 拼进 `system_prompt`
   （merchant 角色另加一句"你正在和商家对话"），把用户消息塞进 `messages`，进入 `run_query`。
3. **run_query 循环**（最多 `max_turns=8` 轮）：
   - 进循环前**按 `role` 过滤可见工具**：`allowed_roles is None or role in allowed_roles` 才进 `tool_schemas`。
   - `should_compact(messages)` 超阈值就 `microcompact` 一下。
   - `client.stream_message(messages, tool_schemas, system_prompt)` 拿模型回复。
   - 没有 tool_use → `yield AssistantTurnComplete` 退出。
   - 有 tool_use → 对每个：**角色门禁兜底**（防瞎编工具名）→ pydantic 校验入参 →
     写操作过 `confirm` 回调 → 调 `tool.execute()` → 用 `ToolResultBlock` 配回
     `tool_use_id` 塞进 messages，继续下一轮。
4. **server 边收边推**：把 engine yield 出来的事件即时翻成 NDJSON 一行行写出去——
   - `ToolExecutionStarted` → `{"type":"status","text":"正在查库存…","tool_name":"...","busy":true}`
   - `ToolExecutionCompleted` → 同上,phase=completed,带 `is_error`
   - `AssistantTurnComplete` → `{"type":"final","reply":"..."}`
   - 异常 → `{"type":"error","message":"..."}`
5. **前端 fetch + ReadableStream + NDJSON** 逐行读：status 事件原地刷气泡文案,
   final 事件触发逐字呈现回复。同一个 assistant 气泡贯穿整轮,不抖。

事件流而不是 print：循环只负责"发生了什么"，外壳（CLI / FastAPI / 前端）自己决定怎么显示。
这就是为什么把"丢掉的中间事件捡起来"只动了 server 30 行、engine 一行没改。

> 还有一个 **`POST /chat`**（非流式）端点保留作 curl 调试用——发一次拿一次 JSON,不需要
> 处理 NDJSON 拆行。前端不走这条路径。

---

## 工具清单（`business/cs_tools.py`）

| 工具 | 类别 | 可见角色 | 作用 |
|---|---|---|---|
| `search_products` | A 只读 | all | 关键词搜商品 |
| `check_stock` | A 只读 | all | 查某商品某尺码库存 |
| `recommend_size` | A 只读 | all | 按身高/体重/品类查尺码表 |
| `get_order_status` | A 只读 | all | 按订单号查状态 |
| `place_order` | **B 写** | all | **发起下单**：建待确认草稿 + 预占库存（`locked+qty`），返回 draft_id；不真扣款，真正生效靠独立确认接口（见"下单确认"一节） |
| `cancel_order` | **B 写** | all | 取消订单 + 退回库存 |
| `write_memory` | A 只读 | all | 模型自己决定要不要把"用户穿42码"这类跨会话信息追加到 `memory_{uid}.md` |
| `update_price` | **B 写** | **merchant** | 改商品价格 |
| `add_product` | **B 写** | **merchant** | 新上架商品（⚠️ 见下方说明，不含初始库存） |

- 写操作（`is_write=True`）传 `confirm` callback 后会被拦住问一句；只读工具自由调。
- 商家工具连 schema 都不会发给非 merchant 的模型 → 模型看不见就不会瞎想。

> ⚠️ **`add_product` 只插 products 表，不动 inventory** —— 新上架商品库存默认为 0，
> 直接演示会出现"上架了但买不了"。要让它能卖，需手动塞库存（SQL）或补一个 `add_inventory` 工具
> （见"接下来"）。演示商家功能时注意这点。

---

## 数据

- **MemoryStore**：进程内字典，启动即用，演示/测试用。
- **SqliteStore**（`shop.db`）：服务跑的就是它。`products / inventory / size_chart / orders / users` 五张表。
  - `inventory.locked`：预占量，`available = qty - locked`。建草稿 `locked+qty`（锁货不扣货）；
    确认 `qty-qty, locked-qty`（预占转真扣）；过期 / 取消 pending 草稿 `locked-qty`（释放预占）；
    取消 created/confirmed 订单 `qty+qty`（退回真库存）。`check_stock` 返回 available。
    ⚠️ 取消必须按订单状态退回正确的"位置"——草稿在 locked、已扣单在 qty，退错地方会让账目凭空增减。
  - `orders.status`：`pending`（草稿，已预占）/ `confirmed`（已确认真扣）/ `cancelled`（取消或过期释放）/
    `created`（路径A 直接下单的终态）；`orders.expires_at`：草稿过期时间戳。
  - `orders.request_id` 唯一索引：客户端重发同一个 `request_id` 只会有一条订单（幂等）。
  - 防超卖靠一条原子 SQL + `rowcount` 判断：路径A `UPDATE qty=qty-? WHERE ... AND qty>=?`，
    路径B 预占 `UPDATE locked=locked+? WHERE ... AND qty-locked>=?`；靠数据库行锁防并发超卖，事务里失败回滚。
  - `users.user_id` 是 PK，写入前统一 `lower()` —— 注册和登录走同一规则，避免 `Alice/alice` 分裂。
  - `check_same_thread=False`：FastAPI 多线程下 sqlite3 必须开。

两个实现共享同一个 `Store` 接口，工具不知道底下是谁。

---

## 认证与角色

- **密码**：`bcrypt`（自带盐 + 自适应代价因子），存哈希字符串。
- **令牌**：JWT HS256，payload `{sub, role, exp}`，24h 有效。`jwt_secret` 从 `.env` 读，缺失就启动失败。
- **`/register`** 永远建 `role='user'`，不接受 role 字段 —— 避免任何人 curl 一下就提权。
- **商家账号**由 `.env` 的 `merchant_user` / `merchant_password` 在启动时种入 users 表，
  改 .env 重启就生效（既有账号会被对齐到 env 声明的密码 + role=merchant）。
- **登录失败**统一返回 `401 账号或密码错误` —— 不区分"账号不存在"和"密码错"，避免账号枚举。

---

## 下单确认：把"危险的一步操作"拆成"安全的多步状态机"

下单是不可逆写操作，控制它有两套机制，分别服务两种入口：

### 机制一 · 引擎级 confirm 回调（CLI）

`run_query` 执行 `is_write` 工具前调 `confirm` 回调，`main.py` 的 `cli_confirm` 用 `input()`
问 y/n，拒绝则把"用户取消"作为 tool_result 塞回。confirm 注入到引擎、不耦合具体交互方式。
**HTTP 路径建 engine 时不传 confirm（`config.confirm is None`），所以这套在 web 流里不触发** ——
web 的安全由机制二兜底。

### 机制二 · 草稿确认状态机（HTTP，本项目主用）

把"下单"从**一个动作**（扣库存+建单，不可逆）改造成**一个状态机**：

```
pending(预占 locked+qty) ──confirm──▶ confirmed(真扣 qty、释放 locked)
                         └─expire──▶ cancelled(释放 locked)
```

每个状态转移独立、可校验、可回滚。它分三层落地，状态全在数据库、请求间无状态：

| 层 | 位置 | 职责 |
|---|---|---|
| **数据层** | `business/store.py` | `inventory.locked` + `orders.expires_at` + 三方法：`create_draft_order`（原子预占 `UPDATE locked=locked+? WHERE qty-locked>=?` + 建 pending + 记过期）、`confirm_draft_order`（校验归属+没过期+幂等 → 预占转真扣）、`release_expired_orders`（过期释放预占+置 cancelled，被 `check_stock`/`confirm` 惰性调用，不起后台任务） |
| **工具层** | `place_order`（`cs_tools.py`） | agent 只负责**发起**：调 `create_draft_order` 建草稿、返回 draft_id + 待确认提示。agent 碰不到"真扣款"那一步 |
| **接口层** | `POST /orders/{id}/confirm` · `POST /orders/{id}/cancel`（`server.py`） | **不经过 agent** 的独立操作，二者对称。confirm 走 `confirm_draft_order`（归属+幂等在 store）；cancel 在端点 fetch 出来比对 `user_id`（store.cancel_order 不带归属，还给 CLI 用）→ `cancel_order`。归属/不存在 → 400；幂等重试（已确认 / 已取消）→ 200 成功。**幂等判断都放在归属校验之后**，否则会幂等返回成功、泄露别人单的存在 |
| **前端层** | `frontend/index.html` | agent 回复带 `draft_id` 时显示"确认下单 / 取消"两个按钮，点击调对应接口；`orderState`（idle→confirming/cancelling→done/cancelled/failed）管按钮显隐，终态后两键消失、处理中禁用防重复点。`draft_id` 由 server 从 `place_order` 的确定性输出抠出、随 `final` 事件透出（不解析 LLM 自由回复） |

**为什么接口层是安全核心**：真正生效（扣库存）只能通过这条绕不过的后端接口 ——
- 模型不听话直接调 `place_order`？没用，只建草稿（pending），不确认就不扣；
- 绕过前端 `curl`？也得带 token、也只能确认自己的草稿、也走同一套后端校验。

> 测试见 `tests/test_draft_order.py`（数据层 7 项）+ `tests/test_confirm_flow.py`（工具+接口 7 项），
> 覆盖：建草稿 available 减 / 确认 qty 真减 / 过期还原 / 越权确认被拒 / 重复确认幂等。

### 两条下单路径并存（设计决策，非遗漏）

`store` 里 `create_order`（**路径A**，一步直接扣库存建 `created` 订单）和上面的草稿状态机
（**路径B**）并存。`place_order` 已切到路径B，路径A 没人在 web 流里调，**有意保留**作为
"一步动作 vs 状态机"的对照基准 —— 删了会让两种模型的差异看不见，且留着不碍事。新功能一律走路径B。

---

## 跑起来

```bash
pip install -r requirements.txt
# .env 至少配：api_key / base_url（OpenAI 兼容端点）/ jwt_secret
# 想要 merchant 账号再加 merchant_user / merchant_password
```

三种入口：

```bash
python main.py                    # CLI（写操作问 y/n，无认证、单用户）
uvicorn server:app --reload       # HTTP + 前端（http://localhost:8000/）
python -m pytest tests/ -v        # 测试（FakeClient，不打真 API）
python tests/smoke_auth.py        # auth + users 表 + 商家工具
python tests/smoke_role_gate.py   # role → 工具门禁端到端
```

前端 `frontend/index.html` 单文件，Vue 落到本地 `frontend/vue.global.prod.js`（不依赖外网 CDN），
由 FastAPI 挂在 `/`。未登录看到登录/注册卡片；登录后 token 存 localStorage（`shop_auth`），
chat 自动加 `Authorization: Bearer ...`；401 自动登出。

---

## 现状 & 接下来

**已跑通**：harness 循环、工具调用、SQLite 持久化、上下文压缩、跨会话记忆、HTTP + 前端、
幂等下单、防超卖事务、bcrypt + JWT 认证、role 门禁（schema 过滤 + 执行兜底）、商家工具、
env-seeded 商家账号、`/chat/stream` 单向事件流（NDJSON）+ 前端工具进度文案 + 逐字回复、
**草稿确认状态机全链路（预占库存 + place_order 建草稿 + 独立确认接口 `/orders/{id}/confirm`
+ server 透出 draft_id + 前端"确认下单"按钮，四层贯通，见"下单确认"一节）**。

**接下来（按价值排）：**
- **工具并行执行**：同一回合里模型发出的多个 tool_use 当前是 `for` 循环串行 await,
  独立工具本可 `asyncio.gather`。改完前端进度区从"一行文案"扩成"活动列表"。
- **草稿过期的后台清理**：当前过期靠 `check_stock`/`confirm` 惰性触发 `release_expired_orders`；
  没人查库存的冷门 SKU 草稿会"悬挂"占着预占。规模化时补一个定时任务兜底（demo 惰性够用）。
- **`add_inventory` 工具**：让商家上架的新品能配库存、真正可卖（当前 `add_product` 不动 inventory）。
  顺便把进度文案搬到 BaseTool 属性上（`progress_started/completed`），server.py 解耦工具语义。
- 连接池：当前单连接 + 单元逻辑串行，幂等的并发兜底（唯一索引）已为多连接准备；规模化时换连接池。
- `sessions` 加 TTL / LRU：当前无上限，在线人数级 demo 够用，规模化要进程外存。

**有意不做的（设计决策，非遗漏）：**
- **不设独立 ErrorEvent**：错误统一用 `AssistantTurnComplete` 表达（上层对"正常结束/错误结束"
  处理一致，都是显示文本，无需区分）。若未来上层要对错误特殊处理（监控/重试），再加。
- **记忆不做检索/打分**：记忆是模型概括的精华（"用户穿42码"这类一句话，几条），全文拼进
  system_prompt 即可，规模小到无需向量检索。
- **不堆更多工具**：现有 9 个工具足以演示读写分级 + 角色门禁两个核心能力；加工具是业务堆砌，
  不增加工程价值。

---

## 设计要点（沉淀）

- **工具属性表示能力，循环里实现策略**：`is_write` / `allowed_roles` 只是 BaseTool 上的属性位，
  "要不要 confirm" / "哪些 role 能见"是 `run_query` 的事。以后加"超 N 元订单二次确认"、
  "VIP 专属工具"也只动循环，工具不变。
- **schema 过滤 + 执行兜底双层**：role 门禁两处生效 —— 发给模型的 schema 按 role 过滤
  （看不到就不会调，省 token、避免"模型尝试 → 被拒 → 重试"），执行前再兜底拦一次（防瞎编工具名）。
- **记忆是模型概括的精华，不是原始对话**：`write_memory` 由模型自己决定调，存一句话不存对话记录。
- **`user_id` 入口就归一化为小写**：注册、登录、JWT sub、文件名、sessions 键、DB PK 全用小写 ——
  避免 `Alice/alice` 在 dict 里是两个 session、却共享同一个 `memory_alice.md`（Windows 文件系统
  不区分大小写，dict 区分）。
- **`.env` 是商家账号的真相源**：放弃"/register 可选 role"和"独立 seed 脚本"，改成启动时按 .env
  对齐 users 表 —— 改一行配置重启就生效，不给客户端提权口子，也不留长期没人跑的脚本。
- **商家工具不按角色条件注册，注册后再过滤**：`build_tools` 一视同仁把所有工具放进 engine，
  "哪些可见"完全交给 `run_query` 的 role 过滤 —— 工具表构造稳定，权限语义集中一处。

---

## 来历

这个项目源自对一个生产级 agent harness（数万行）的源码精读 —— 识别出其核心机制
（循环、工具调度、上下文压缩、记忆、权限），剥离了沙箱、子 agent、记忆检索、多 provider、
checkpoint 等"为它的规模和场景准备、但客服场景不需要"的周边，用约 2000 行实现核心，
再落地下单安全、服务化、多用户、角色门禁。
**取舍的依据始终是"这东西解决的问题，我的场景有吗"，而不是"它有所以我也加"。**
