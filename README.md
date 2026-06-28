# 客服 Agent · 自研 harness 引擎

> 一个从零实现的电商客服 Agent —— 不依赖 LangChain / LlamaIndex，自己写了一套约 2000 行的轻量 harness。
> 核心命题：**当 Agent 能操作真实业务系统（下单、改价、退库存）时，怎么把它的权限和不可逆操作管住。**

模型本身只会「输入文字 → 输出文字 / 工具调用」。把它变成一个能查库存、下单、记住用户的 Agent，
中间那层工程代码就是 **harness**：负责循环调度、工具调用、上下文管理、权限控制、跨会话记忆。
本项目实现了这五样，与具体业务解耦，再在上面落地了一个最小可用的店面。

---

## ✨ 功能一览

**买家**（普通用户）
- 关键词搜商品、查某尺码库存、枚举全部尺码库存、按身高体重推荐尺码
- 下单（草稿确认状态机，见下文）、取消订单、查订单状态
- 跨会话记住尺码偏好（「我穿 42 码」→ 下次进来还记得）

**商家**（独立角色）
- 改价、上架新品、补货 —— 这些工具普通用户的模型**根本看不到**

**工程能力**
- 🔁 自研对话循环：工具调用、pydantic 入参校验、多轮 tool-use
- 🔐 两层权限：只读/写操作分级（写操作需确认）+ 角色门禁（敏感工具按 role 隔离）
- 🧾 草稿确认状态机：把「下单」这步不可逆操作拆成可校验、可回滚的多步
- 🧠 keyed 跨会话记忆：同一类信息 upsert 覆盖，改主意不会自相矛盾
- 🪪 认证：bcrypt 密码 + JWT，role 从 token 一路穿透到工具层
- 🌊 流式前端：`/chat/stream` 推 NDJSON 事件，前端实时显示「正在查库存…」+ 逐字回复
- 🧮 防超卖、下单幂等、防记忆投毒（写入 key 白名单）、上下文压缩、聊天记录持久化
- ✅ 双层测试：pytest 单元/冒烟 + 一套「确定性 vs 概率性」分离的 eval harness

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 `.env`

在项目根目录建一个 `.env`（已被 `.gitignore` 忽略）：

```ini
# —— 模型（OpenAI 兼容端点）——
api_key=sk-xxxx
base_url=https://api.openai.com/v1     # 或任意兼容端点
model=gpt-4o-mini                      # 模型名
api_timeout=60                         # 可选，秒

# —— 认证 —— （缺失会启动失败，防止裸奔签 token）
jwt_secret=请换成一串随机长字符串

# —— 商家账号（可选）—— 启动时按这里种入 / 对齐 users 表
merchant_user=boss
merchant_password=boss1234
```

### 3. 三种入口任选

```bash
# ① CLI：纯命令行，写操作问 y/n，无认证、单用户
python main.py

# ② HTTP + 前端：浏览器打开 http://localhost:8000/
uvicorn server:app --reload
#   API 文档在 http://localhost:8000/docs

# ③ 测试与评估
python -m pytest tests/ -v        # 单元/冒烟（FakeClient，不打真 API）
python -m eval.eval_safety_invariants   # 某个 eval 维度（需真实模型，或加 EVAL_FAKE=1 冒烟）
```

前端是单文件 `frontend/index.html` + 本地 `vue.global.prod.js`（不依赖外网 CDN），由 FastAPI 挂在 `/`。
未登录看到登录/注册卡片；登录后 token 存 `localStorage`，chat 自动带 `Authorization: Bearer ...`，401 自动登出。

> 演示账号：`/register` 自助注册的永远是普通用户；商家账号只能由 `.env` 种入（防越权）。

---

## 🏗️ 架构

```
frontend/index.html          单页 Vue：登录/注册 + 对话 UI，token 存 localStorage
       │  POST /register · /login          →  JWT
       │  POST /chat/stream  Bearer <t>    →  NDJSON 事件流：status / final / error
       │  POST /orders/{id}/confirm·cancel →  独立确认/取消（不经 agent，校验归属）
       ▼
server.py                    FastAPI 外壳：解 JWT 拿 (user_id, role) → 取/建 engine →
                             把 engine 的事件流即时翻成 NDJSON 推给前端
                             sessions key = (user_id, role)，避免缓存串色
       ▼
core/  ── harness 引擎（通用，与业务无关）
  messages.py   TextBlock / ToolUseBlock / ToolResultBlock / ConversationMessage
  events.py     AssistantTurnComplete / ToolExecutionStarted / ToolExecutionCompleted
  client.py     ModelClient 协议 + OpenAIClient + FakeClient（测试用）
  tools.py      BaseTool 基类（is_write + allowed_roles）+ ToolResult
  compact.py    超阈值时把旧 tool_result 压成占位符（保留最近若干条）
  memory.py     keyed 跨会话记忆：upsert_memory(key,value) 同 key 覆盖
  auth.py       bcrypt 哈希 + JWT 签发/校验
  engine.py     RunConfig（含 role）+ run_query（按 role 过滤工具）+ QueryEngine
       ▼
business/ ── 客服业务
  store.py      Store 接口 + MemoryStore + SqliteStore（products/inventory/size_chart/orders/users）
  cs_tools.py   11 个工具（见下表），全部只调 Store 接口
       ▼
main.py                      CLI 入口（同样接 QueryEngine，只是输出方式不同）
tests/ · eval/               FakeClient 驱动的测试 + 评估 harness
```

**四条设计原则**

1. **内部统一格式 + client 翻译**：harness 里只有 `ConversationMessage / Block`，各 provider 的 client 负责翻成对方格式。
2. **工具只调 Store 接口**：换底层存储（字典 → SQLite → …）工具一行不改。
3. **工具属性标记 + 策略在循环里**：`is_write=True` 决定要不要 confirm；`allowed_roles={"merchant"}` 决定哪些角色能看到。工具本身不知道自己被特殊对待。
4. **role 一路穿透**：JWT `{sub, role, exp}` → `decode_token` → `get_engine(user_id, role)` → `RunConfig.role` → `run_query` 里按 role 过滤工具 schema（模型根本看不到用不了的工具）+ 执行前再兜底拦一次。

---

## 🧰 工具清单（`business/cs_tools.py`）

| 工具 | 类别 | 可见角色 | 作用 |
|---|---|---|---|
| `search_products` | 只读 | all | 关键词搜商品 |
| `check_stock` | 只读 | all | 查某商品某尺码库存（返回 available） |
| `list_stock` | 只读 | all | 枚举某商品的**全部真实尺码**及库存（防尺码幻觉） |
| `recommend_size` | 只读 | all | 按身高/体重/品类查尺码表 |
| `get_order_status` | 只读 | all | 按订单号查状态 |
| `place_order` | **写** | all | 发起下单：建待确认草稿 + 预占库存，返回 `draft_id`；**不真扣款** |
| `cancel_order` | **写** | all | 取消订单 + 退回库存 |
| `write_memory` | 只读 | all | 记住用户尺码偏好（`key`+`value`，同 key 覆盖） |
| `update_price` | **写** | **merchant** | 改商品价格 |
| `add_product` | **写** | **merchant** | 新上架商品（⚠️ 不含初始库存，需配合 `restock_product`） |
| `restock_product` | **写** | **merchant** | 给已有商品某尺码补货 |

- 写操作（`is_write=True`）在 CLI 路径会被 `confirm` 回调拦一句；只读工具自由调。
- 商家工具连 schema 都不会发给非 merchant 的模型 —— 模型看不见就不会瞎想。

> ⚠️ `add_product` 只插 products 表、不动 inventory，新品库存默认 0；要能卖得先 `restock_product` 配库存。

---

## 🔒 核心机制

### 两层权限边界
- **读写分级**：只读工具（查库存、推荐尺码）Agent 自由调；写操作（下单、取消）需确认，防误操作。
- **角色门禁**：敏感工具（改价、上架、补货）按角色隔离。门禁两处生效 ——
  发给模型的 schema 按 role 过滤（看不到就不会调，省 token），执行前再兜底拦一次（防瞎编工具名）。

### 下单确认：把「危险的一步」拆成「安全的状态机」
下单是不可逆写操作，Web 路径用一个状态机控制它：

```
pending(预占 locked+qty) ──confirm──▶ confirmed(真扣 qty、释放 locked)
                         └─expire──▶ cancelled(释放 locked)
```

分四层落地，状态全在数据库、请求间无状态：

| 层 | 位置 | 职责 |
|---|---|---|
| 数据层 | `store.py` | `inventory.locked` + `orders.expires_at`；原子预占、确认转真扣、过期释放 |
| 工具层 | `place_order` | Agent 只**发起**：建草稿、返回 `draft_id`，碰不到「真扣款」那一步 |
| 接口层 | `POST /orders/{id}/confirm·cancel` | **不经 Agent** 的独立操作，校验登录 + 归属 + 幂等；真扣库存只在这里发生 |
| 前端层 | `index.html` | Agent 回复带 `draft_id` 时显示「确认下单 / 取消」按钮，点击调对应接口 |

**为什么接口层是安全核心**：真正扣库存只能走这条绕不过的后端接口 —— 模型不听话直接调 `place_order` 也只建草稿；
绕过前端 `curl` 也得带 token、也只能确认自己的草稿、也走同一套校验。

### 跨会话记忆（keyed upsert）
- 存 `memory_{user_id}.md`（内部 JSON：`{key: {value, updated_at}}`），随 `user_id` 隔离。
- 同一类信息用同一个 `key` **覆盖**（如尺码 `shoe_size`）——「先说 42、后改 43」只会留下最新的 43，不再 append-only 自相矛盾。
- 记忆是模型概括的精华（一句话，几条），会话开始全文拼进 `system_prompt`，规模小到无需向量检索。
- **防投毒护栏**：记忆会跨会话注入 `system_prompt`，是持久污染的攻击面。写入路径强制 key 白名单（`ALLOWED_MEMORY_KEYS={shoe_size, top_size}`），非白名单（如被诱导写 `role=merchant`、`discount=1折`）一律拒绝、不落盘；`value` 另加 `max_length` 挡超长注入文本。**拦在工具层（代码约束），不靠 prompt** —— prompt 拦不住对抗输入。

### 其它保障
- **防超卖**：靠一条原子 SQL + `rowcount` 判断（`UPDATE ... WHERE qty-locked>=?`），数据库行锁挡并发。
- **下单幂等**：`orders.request_id` 唯一索引；同一轮重复下单收敛成一张草稿。
- **认证**：bcrypt 存哈希；JWT HS256，payload `{sub, role, exp}` 24h 有效；登录失败统一 `401`，不区分「账号不存在/密码错」防枚举。
- **聊天记录**：`/chat/stream` 落库用户消息 + Agent 最终回复（工具过程不存）；`GET /history` 只查自己的、支持上拉分页。
- **大小写归一**：`user_id` 入口统一 `lower()`，避免 `Alice/alice` 在 session 字典里分裂却共享同一记忆文件。

---

## ✅ 测试与评估

```bash
python -m pytest tests/ -v          # 单元/冒烟：FakeClient 驱动，不打真 API
python tests/smoke_auth.py          # auth + users 表 + 商家工具
python tests/smoke_role_gate.py     # role → 工具门禁端到端
```

`eval/` 是一套独立的评估 harness，刻意把**确定性**（代码机制保证，阈值 1.0）与**概率性**（模型行为，多 trials + 统计阈值）分开评，避免一次随机抖动拖垮整次跑：

| 维度 | 脚本 | 性质 |
|---|---|---|
| 工具选择准确率 | `eval_tool_selection.py` | 概率 |
| 安全·不变量（角色门禁/草稿状态机） | `eval_safety_invariants.py` | 确定 |
| 安全·跨用户越权隔离 | `eval_safety_ownership.py` | 确定 |
| 安全·对抗拦截 | `eval_safety_adversarial.py` | 概率 |
| 任务完成正确性 | `eval_task_correctness.py` | 概率 |
| 数据层 / Agent 并发 | `eval_concurrency*.py` | 确定/概率 |
| 记忆一致性（同 key 覆盖）+ 跨用户越权隔离 | `eval_memory.py` | 确定 |

```bash
python -m eval.eval_safety_invariants            # 真实模型跑
EVAL_FAKE=1 python -m eval.eval_tool_selection   # 冒烟：只跑可脚本化的 case，零 API
python -m eval.eval_tool_selection --trials 3    # 概率维度多跑几次看稳定性
```

详见 [`eval/README.md`](eval/README.md)。

---

## 🗺️ 未来优化点

按价值排序：

- **工具并行执行**：同一回合里多个 `tool_use` 当前是 `for` 串行 await，独立工具本可 `asyncio.gather`；改完前端进度区从「一行文案」扩成「活动列表」。
- **草稿过期后台清理**：当前过期靠 `check_stock`/`confirm` 惰性触发释放；冷门 SKU 的草稿会悬挂占预占，规模化时补一个定时任务兜底。
- **`add_inventory` / 上架即配库存**：让商家新上架商品能直接配库存、真正可卖；顺便把进度文案搬到 `BaseTool` 属性上，让 server 解耦工具语义。
- **连接池**：当前单 SQLite 连接 + 逻辑串行；幂等的并发兜底（唯一索引）已为多连接准备，规模化时换连接池。
- **`sessions` 加 TTL / LRU**：当前内存无上限，在线人数级 demo 够用，规模化要进程外存（Redis 等）。
- **记忆写并发加锁**：`upsert_memory` 是 `读→改→写` 非原子序列，同一用户并发写不同 key 会 last-write-wins 丢更新。单用户并发概率极低，demo 可接受；生产需文件锁或行级存储（与 store 层 `RLock` 对齐）。
- **多 provider**：`ModelClient` 是协议，目前只接了 OpenAI 兼容端点，可补 Anthropic 等的翻译层。

**有意不做（设计取舍，非遗漏）**
- 不设独立 `ErrorEvent`：错误统一用 `AssistantTurnComplete` 表达，上层处理一致。
- 记忆不做检索/打分：精华小到全文拼进 prompt 即可。
- 不堆更多工具：现有 11 个足以演示读写分级 + 角色门禁两个核心能力。

---

## 📐 设计要点（沉淀）

- **工具属性表示能力，循环里实现策略**：`is_write` / `allowed_roles` 只是属性位；「要不要 confirm」「哪些 role 能见」是 `run_query` 的事。以后加「超 N 元二次确认」「VIP 专属工具」也只动循环。
- **schema 过滤 + 执行兜底双层**：role 门禁两处生效，省 token 又防瞎编工具名。
- **安全约束落代码层、不落 prompt**：role 门禁、下单真扣库存、记忆 key 白名单都是代码硬拦；prompt 只做「指引」，对抗输入下指引会被绕过，约束不会。
- **`.env` 是商家账号的真相源**：启动时按 `.env` 对齐 users 表，改配置重启即生效，不给客户端提权口子。
- **两条下单路径并存**：`store.create_order`（路径 A，一步直接扣库存）有意保留作为「一步动作 vs 状态机」的对照基准；新功能一律走路径 B（草稿状态机）。

---

## 📖 来历

本项目源自对一个生产级 Agent harness（数万行）的源码精读 —— 识别其核心机制（循环、工具调度、上下文压缩、记忆、权限），
剥离掉沙箱、子 Agent、记忆检索、多 provider、checkpoint 等「为它的规模和场景准备、但客服场景不需要」的周边，
用约 2000 行实现核心，再落地下单安全、服务化、多用户、角色门禁。

**取舍的依据始终是「这东西解决的问题，我的场景有吗」，而不是「它有所以我也加」。**
