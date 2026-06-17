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
       │  POST /register · /login  →  JWT
       │  POST /chat   Authorization: Bearer <token>   { message }
       ▼
server.py                    FastAPI 外壳
                               · 启动时按 .env 种 merchant 账号
                               · /register 永远建 role='user'
                               · /chat 解 JWT 拿 (user_id, role) → 取/建 engine
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

## 一次 `/chat` 发生了什么

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
4. **server** 只收 `AssistantTurnComplete` 事件，把文本返给前端。CLI 还会打印工具执行事件。

事件流而不是 print：循环只负责"发生了什么"，外壳（CLI / FastAPI / 前端）自己决定怎么显示。

---

## 工具清单（`business/cs_tools.py`）

| 工具 | 类别 | 可见角色 | 作用 |
|---|---|---|---|
| `search_products` | A 只读 | all | 关键词搜商品 |
| `check_stock` | A 只读 | all | 查某商品某尺码库存 |
| `recommend_size` | A 只读 | all | 按身高/体重/品类查尺码表 |
| `get_order_status` | A 只读 | all | 按订单号查状态 |
| `place_order` | **B 写** | all | 下单（`UPDATE qty - ? WHERE qty >= ?` 防超卖，`request_id` 唯一索引兜底幂等） |
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
  - `orders.request_id` 唯一索引：客户端重发同一个 `request_id` 只会有一条订单（幂等）。
  - 下单用 `UPDATE inventory SET qty = qty - ? WHERE ... AND qty >= ?` + `rowcount` 判断：
    检查与扣减合并为一条原子 SQL，靠数据库行锁防并发超卖；扣库存 + 插订单包在事务里，失败回滚。
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

## 下单确认：机制与落地状态

"写操作需确认"是本项目的核心安全机制，落地分两条路径：

- **CLI（已完整接入）**：`run_query` 执行写操作前调 `confirm` 回调，`main.py` 的 `cli_confirm`
  用 `input()` 问 y/n，拒绝则把"用户取消"作为 tool_result 塞回。**确认机制在 CLI 下完整可演示。**
- **HTTP（机制就绪，未接前端交互）**：HTTP 是一次请求一次响应，没法像 CLI 那样在请求中途阻塞等用户敲键。
  完整落地需要把 `ToolExecutionStarted` 推给前端、前端按钮回写确认、`run_query` 暂停等待 ——
  即把"事件流"从单向 yield 改成双向。这是已规划的下一步，机制（confirm 回调注入）已就位，
  接的是交互方式。

> 设计上 confirm 是注入到 `run_query` 的回调，引擎不耦合具体交互方式 —— 这正是 CLI 用 input、
> HTTP 改两步式时，引擎一行不用改的原因。

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
env-seeded 商家账号。

**接下来（按价值排）：**
- **HTTP 下单两步式确认**：把核心安全机制搬上界面（机制已就绪，见上节）。
- **`add_inventory` 工具**：让商家上架的新品能配库存、真正可卖（当前 `add_product` 不动 inventory）。
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
