# 客服 Agent（自研 harness 引擎）

一个从零实现的电商客服 agent。不依赖 LangChain，核心是自己写的轻量 harness：
**模型 ↔ 工具的循环 + 事件流 + 消息格式翻译 + 上下文压缩 + 跨会话记忆**。

业务上是个最小可用的店面：搜商品、查库存、推荐尺码、下单、取消、查订单状态；
界面是单文件 Vue + FastAPI 后端，每个用户一个独立的 engine 会话，记忆按 `user_id` 落到本地 `memory_{uid}.md`。

---

## 分层

```
frontend/index.html      单页 Vue，对话 UI（已成型）
       │
       │  POST /chat   { user_id, message }
       ▼
server.py                FastAPI 外壳：维护 sessions[user_id] → QueryEngine
       │                store 全局只建一次（公共数据），engine 按用户隔离
       ▼
core/  ── harness 引擎（通用，与业务无关）
  messages.py   TextBlock / ToolUseBlock / ToolResultBlock / ConversationMessage
  events.py     AssistantTurnComplete / ToolExecutionStarted / ToolExecutionCompleted
  client.py     ModelClient 协议 + OpenAIClient + FakeClient（测试用）
  tools.py      BaseTool 基类 + ToolResult；is_write 区分只读/写操作
  compact.py    超阈值时把旧的 tool_result 清成占位符（保留最近 5 条）
  memory.py     读 / 追加 memory_{user_id}.md
  engine.py     RunConfig + run_query（循环）+ QueryEngine（会话管理器）
       │
       ▼
business/ ── 客服业务
  store.py      Store 接口 + MemoryStore（字典）+ SqliteStore（shop.db）
  cs_tools.py   7 个工具（见下文），全部只调 Store 接口
       │
       ▼
main.py                  CLI 入口（同样接 QueryEngine，只是输出方式不同）
tests/                   FakeClient 驱动的单元测试
```

设计原则三条：

- **内部统一格式 + client 翻译**：harness 里只有 `ConversationMessage / Block`，
  各 provider 的 client 负责把它翻译成对方的格式（`OpenAIClient._to_openai` / `_from_openai`）。
- **工具只调 Store 接口**：换底层存储（字典→SQLite→…）工具一行不改。
- **写操作标记 `is_write=True`**：阶段5 的权限确认据此判断要不要拦下来问用户。
  CLI 已经接上 `cli_confirm`；HTTP 服务暂时不传 `confirm`（=放行所有写操作）。

---

## 一次 `/chat` 发生了什么

1. **server.py** 拿 `user_id` 找/建 `QueryEngine`，调 `submit_message(text)`。
2. **engine** 把当前用户的 `memory_{uid}.md` 拼进 `system_prompt`，把用户消息塞进 `messages` 历史，进入 `run_query`。
3. **run_query 循环**（最多 `max_turns=8` 轮）：
   - `should_compact(messages)` 超阈值就 `microcompact` 一下。
   - `client.stream_message(messages, tools, system_prompt)` 拿模型回复。
   - 没有 tool_use → `yield AssistantTurnComplete` 退出。
   - 有 tool_use → 对每个：pydantic 校验入参 → 写操作过 `confirm` → 调 `tool.execute()` →
     用 `ToolResultBlock` 配回 `tool_use_id` 塞进 messages，继续下一轮。
4. **server** 只收 `AssistantTurnComplete` 事件，把文本返给前端。
   CLI 还会顺手把 `ToolExecutionStarted/Completed` 打出来。

事件流而不是 print：循环只负责"发生了什么"，外壳（CLI / FastAPI / 前端）自己决定怎么显示。

---

## 工具清单（`business/cs_tools.py`）

| 工具 | 类别 | 作用 |
|---|---|---|
| `search_products` | A 只读 | 关键词搜商品 |
| `check_stock` | A 只读 | 查某商品某尺码库存 |
| `recommend_size` | A 只读 | 按身高/体重/品类查尺码表 |
| `get_order_status` | A 只读 | 按订单号查状态 |
| `place_order` | **B 写** | 下单（SqliteStore 用 `UPDATE qty - ? WHERE qty >= ?` 防超卖，`request_id` 唯一索引兜底幂等） |
| `cancel_order` | **B 写** | 取消订单 + 退回库存 |
| `write_memory` | A 只读 | 让模型自己决定要不要把"用户穿42码"这种跨会话信息追加到 `memory_{uid}.md` |

写操作传 `confirm` callback 后会被拦住问一句；只读工具 agent 自由调。

---

## 数据

- **MemoryStore**：进程内字典，启动即用，演示/测试用。
- **SqliteStore**（`shop.db`）：服务跑的就是它。建表 + 初始数据 + 幂等下单都在 `_init_db / create_order` 里。
  - `orders.request_id` 唯一索引：客户端重发同一个 `request_id` 只会有一条订单。
  - `check_same_thread=False`：FastAPI 多线程下 sqlite3 必须开。

两个实现共享同一个 `Store` 接口，工具不知道底下是谁。

---

## 跑起来

```bash
pip install -r requirements.txt
# .env 里配 api_key 和 base_url（OpenAI 兼容端点都行）
```

三种入口任选：

```bash
# CLI（写操作会问 y/n）
python main.py

# HTTP + 前端（访问 http://localhost:8000/）
uvicorn server:app --reload

# 测试（FakeClient，不打真 API）
python -m pytest tests/ -v
```

前端是 `frontend/index.html` 单文件，由 FastAPI 直接挂在 `/` 提供，`POST /chat` 走同一服务。

---

## 现状 & 接下来

已经能跑通：循环、工具调用、SQLite 持久化、上下文压缩、跨会话记忆、HTTP + 前端、幂等下单。

还没补的：
- 阶段3 的"错误回退"事件类型（无货→自动转推荐）目前靠模型自己接，没单独的 `ErrorEvent`。
- 阶段5 的权限确认在 HTTP 这条路径没接（CLI 已接 `cli_confirm`）；要做的话需要把
  `ToolExecutionStarted` 推给前端、前端按钮回写、`run_query` 暂停等待——
  涉及把"事件流"从单向 yield 改成双向。

---

## 设计要点（沉淀）

- **写操作分级**只是 `is_write` 一个布尔位，权限策略在循环里实现，工具本身不知道自己被"特殊对待"——
  以后加"超过 N 元订单二次确认"这种规则也是改循环，工具不动。
- **记忆是模型概括的精华，不是原始对话**：`write_memory` 由模型自己决定调，存的是
  "用户穿42码鞋"这种一句话，不存对话记录。读取时全文拼进 system_prompt，
  规模小到没必要做检索/打分。
- **`user_id` 归一化为小写**：避免 `Alice` / `alice` 在 dict 里是两个 session、
  却共享同一个 `memory_alice.md` 文件（Windows 文件系统不区分大小写，dict 区分）。
