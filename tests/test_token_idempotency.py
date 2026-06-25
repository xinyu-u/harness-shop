"""创建侧「按对话轮次」令牌幂等的验证。

三层覆盖：
  1. store 层：同令牌只建一张草稿、只锁一份（MemoryStore + SqliteStore 行为一致）
  2. 端到端：模型同一 turn 双发 place_order → 收敛成一张草稿
  3. 并发隔离：同 engine 上并发两轮、不同令牌互不串台（ContextVar per-task）

注：async 用例用 asyncio.run 包成同步 def——本仓库 pytest 未开 asyncio 自动模式，
裸 async def test_ 会被跳过。

跑法（项目根目录）：python -m pytest tests/test_token_idempotency.py -v
"""

import asyncio

from business.store import MemoryStore, SqliteStore
from business.cs_tools import build_tools
from core.client import FakeClient
from core.engine import QueryEngine
from core.messages import ConversationMessage, ToolUseBlock, TextBlock
from core.tools import request_token_var


# ───────── 1. store 层：同令牌一张草稿 ─────────

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
    assert store.check_stock("airmax", "42") == 3   # 两份都锁


def test_no_token_still_works():
    store = MemoryStore()
    d = store.create_draft_order("airmax", "42", 1, "alice")   # 不传令牌不报错
    assert d["status"] == "pending"


def test_sqlite_same_token_dedupes_draft():
    # 主路径用 SqliteStore：行为必须和 MemoryStore 一致（约束6）
    store = SqliteStore(":memory:")
    d1 = store.create_draft_order("airmax", "42", 1, "alice", request_token="tok-1")
    d2 = store.create_draft_order("airmax", "42", 1, "alice", request_token="tok-1")
    assert d1["id"] == d2["id"]
    assert store.check_stock("airmax", "42") == 4
    # 不传令牌的多张草稿不被唯一索引误杀（NULL 互不相等）
    a = store.create_draft_order("airmax", "42", 1, "bob")
    b = store.create_draft_order("airmax", "42", 1, "bob")
    assert a["id"] != b["id"]


# ───────── 2. 端到端：同一 turn 双发收敛成一张 ─────────

def test_double_fire_in_one_turn_dedupes():
    async def _run():
        store = MemoryStore()
        # 一条 assistant 回复里塞两个 place_order（模型同 turn 双发）
        scripted = [
            ConversationMessage(role="assistant", content=[
                ToolUseBlock(name="place_order", input={"product_id": "airmax", "size": "42", "qty": 1}),
                ToolUseBlock(name="place_order", input={"product_id": "airmax", "size": "42", "qty": 1}),
            ]),
            ConversationMessage(role="assistant", content=[TextBlock(text="已为你下单")]),
        ]
        engine = QueryEngine(
            FakeClient(scripted=scripted), build_tools(store, "alice"), user_id="alice"
        )
        _ = [e async for e in engine.submit_message("买双 airmax 42", request_token="turn-1")]
        return store

    store = asyncio.run(_run())
    pendings = [o for o in store._orders if o["status"] == "pending"]
    assert len(pendings) == 1, f"同 turn 双发应只建一张草稿，实际 {len(pendings)}"
    assert store.check_stock("airmax", "42") == 4, "应只锁 1 份"


# ───────── 3. 并发隔离：不同令牌互不串台 ─────────

def test_token_var_isolated_across_concurrent_turns():
    async def _run():
        seen = {}

        async def turn(name, token):
            request_token_var.set(token)
            await asyncio.sleep(0)        # 让出控制权，逼两个协程在此交错
            seen[name] = request_token_var.get()

        # gather 把每个协程包成独立 Task，各自复制上下文 → .set 互不串扰
        await asyncio.gather(turn("A", "tokA"), turn("B", "tokB"))
        return seen

    seen = asyncio.run(_run())
    assert seen == {"A": "tokA", "B": "tokB"}, f"令牌串台了：{seen}"
