"""步骤2 + 步骤3 验证：place_order 改建草稿 + 独立确认接口。

步骤2（工具层）：agent 调 place_order → 只建草稿（pending + 预占），不真扣库存，
                返回里带 draft_id + 待确认提示。
步骤3（接口层）：POST /orders/{id}/confirm —— 不经过 agent，校验 token + 归属 +
                幂等，真正扣库存只能走这条绕不过的后端接口。

跑法（项目根目录）：python -m tests.test_confirm_flow
"""

import asyncio

from fastapi import HTTPException

from business.store import SqliteStore
from business.cs_tools import PlaceOrderTool, PlaceOrderInput
from core.auth import create_token
import server


def _raw_inventory(store, product_id, size):
    row = store._conn.execute(
        "SELECT qty, locked FROM inventory WHERE product_id = ? AND size = ?",
        (product_id, size),
    ).fetchone()
    return (row[0], row[1])


# ════════════════ 步骤2：place_order 工具改成建草稿 ════════════════

def test_place_order_creates_draft_not_real_order():
    store = SqliteStore(":memory:")
    tool = PlaceOrderTool(store, user_id="alice")

    result = asyncio.run(tool.execute(PlaceOrderInput(product_id="airmax", size="42", qty=1)))

    assert not result.is_error, f"建草稿应成功：{result.output}"
    # 订单是 pending 草稿，不是已扣库存的 created
    order = store.get_order(1)
    assert order is not None and order["status"] == "pending", "place_order 应建 pending 草稿"
    # 只预占、不真扣
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert qty == 5, "建草稿不真扣 qty"
    assert locked == 1, "建草稿预占 locked+1"
    # 返回里带上 draft_id + 待确认提示（前端/用户据此触发确认）
    assert str(order["id"]) in result.output, "返回应带 draft_id"
    assert "确认" in result.output, "返回应有待确认提示"
    print("✅ 步骤2 建草稿：place_order 只预占、返回 draft_id + 待确认提示")


def test_place_order_insufficient_stock():
    store = SqliteStore(":memory:")
    tool = PlaceOrderTool(store, user_id="alice")

    result = asyncio.run(tool.execute(PlaceOrderInput(product_id="airmax", size="43", qty=1)))  # 43码库存0

    assert result.is_error, "库存不足应返回错误"
    print("✅ 步骤2 库存不足：建草稿被拒、返回错误")


# ════════════════ 步骤3：独立确认接口 /orders/{id}/confirm ════════════════

def _fresh_server_store():
    """把 server 的全局 store 换成隔离的内存库，避免污染 shop.db。"""
    server.store = SqliteStore(":memory:")
    return server.store


def test_confirm_endpoint_deducts_stock():
    store = _fresh_server_store()
    draft = store.create_draft_order("airmax", "42", 2, "alice")
    token = create_token("alice", "user")

    resp = server.confirm_order(draft["id"], f"Bearer {token}")

    assert resp.status == "confirmed", "确认后状态 confirmed"
    assert resp.order_id == draft["id"]
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert qty == 3 and locked == 0, "确认走后端接口 → qty 真减、预占归还"
    print("✅ 步骤3 确认接口：本人确认 → qty 真减")


def test_confirm_endpoint_rejects_wrong_owner():
    store = _fresh_server_store()
    draft = store.create_draft_order("airmax", "42", 1, "alice")
    token = create_token("bob", "user")   # bob 拿自己的 token 确认 alice 的草稿

    try:
        server.confirm_order(draft["id"], f"Bearer {token}")
        assert False, "越权确认应被拒"
    except HTTPException as e:
        assert e.status_code == 400, "越权 → 400"
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert (qty, locked) == (5, 1), "越权确认不扣库存、预占仍在"
    print("✅ 步骤3 越权确认：被后端拦下（绕过前端 curl 也没用）")


def test_confirm_endpoint_requires_auth():
    _fresh_server_store()
    try:
        server.confirm_order(1, None)   # 不带 token
        assert False, "无 token 应被拒"
    except HTTPException as e:
        assert e.status_code == 401, "缺 token → 401"
    print("✅ 步骤3 无凭证：必须登录才能确认")


def test_confirm_endpoint_nonexistent():
    _fresh_server_store()
    token = create_token("alice", "user")
    try:
        server.confirm_order(999, f"Bearer {token}")
        assert False, "确认不存在的草稿应被拒"
    except HTTPException as e:
        assert e.status_code == 400, "不存在 → 400"
    print("✅ 步骤3 不存在：确认不存在的草稿被拒")


def test_confirm_endpoint_idempotent():
    store = _fresh_server_store()
    draft = store.create_draft_order("airmax", "42", 2, "alice")
    token = create_token("alice", "user")

    server.confirm_order(draft["id"], f"Bearer {token}")
    resp2 = server.confirm_order(draft["id"], f"Bearer {token}")   # 再确认一次

    assert resp2.status == "confirmed"
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert qty == 3 and locked == 0, "重复确认不重复扣（5→3，只减一次）"
    print("✅ 步骤3 幂等：重复确认不重复扣")


# ════════════════ 步骤B：独立取消接口 /orders/{id}/cancel ════════════════
# 与 confirm 接口对称：必须登录 + 校验归属（只能取消自己的单），不经过 agent。
# 归属检查不能漏，否则别人能取消你的单。

def test_cancel_endpoint_releases_pending():
    store = _fresh_server_store()
    draft = store.create_draft_order("airmax", "42", 1, "alice")
    token = create_token("alice", "user")

    resp = server.cancel_order(draft["id"], f"Bearer {token}")

    assert resp.status == "cancelled"
    assert resp.order_id == draft["id"]
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert (qty, locked) == (5, 0), "本人取消 pending → 释放预占、qty 不动"
    print("✅ 步骤B 取消接口：本人取消草稿 → 释放预占")


def test_cancel_endpoint_rejects_wrong_owner():
    store = _fresh_server_store()
    draft = store.create_draft_order("airmax", "42", 1, "alice")
    token = create_token("bob", "user")    # bob 拿自己的 token 取消 alice 的单

    try:
        server.cancel_order(draft["id"], f"Bearer {token}")
        assert False, "越权取消应被拒"
    except HTTPException as e:
        assert e.status_code == 400, "越权 → 400"
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert (qty, locked) == (5, 1), "越权取消不动库存、预占仍在"
    assert store.get_order(draft["id"])["status"] == "pending", "草稿仍是 pending"
    print("✅ 步骤B 越权取消：被后端拦下、单不受影响")


def test_cancel_endpoint_requires_auth():
    _fresh_server_store()
    try:
        server.cancel_order(1, None)
        assert False, "无 token 应被拒"
    except HTTPException as e:
        assert e.status_code == 401, "缺 token → 401"
    print("✅ 步骤B 无凭证：必须登录才能取消")


def test_cancel_endpoint_nonexistent():
    _fresh_server_store()
    token = create_token("alice", "user")
    try:
        server.cancel_order(999, f"Bearer {token}")
        assert False, "取消不存在的单应被拒"
    except HTTPException as e:
        assert e.status_code == 400, "不存在 → 400"
    print("✅ 步骤B 不存在：取消不存在的单被拒")


def test_cancel_endpoint_idempotent():
    """重试取消（已 cancelled）→ 返回成功、不报失败、不二次释放预占。"""
    store = _fresh_server_store()
    draft = store.create_draft_order("airmax", "42", 1, "alice")
    token = create_token("alice", "user")

    server.cancel_order(draft["id"], f"Bearer {token}")          # 第一次：cancelled，释放预占
    resp = server.cancel_order(draft["id"], f"Bearer {token}")   # 重试

    assert resp.status == "cancelled", "重试应返回成功（不抛 400）"
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert (qty, locked) == (5, 0), "重试不二次释放预占（locked 不会变负）"
    print("✅ 步骤B 幂等取消：重试返回成功、不二次释放")


def test_cancel_idempotent_does_not_bypass_ownership():
    """关键：幂等放在归属校验之后 —— 别人的已取消单仍被拒，不会幂等返回成功泄露信息。"""
    store = _fresh_server_store()
    draft = store.create_draft_order("airmax", "42", 1, "alice")
    server.cancel_order(draft["id"], f"Bearer {create_token('alice', 'user')}")  # alice 先取消自己的

    token_bob = create_token("bob", "user")
    try:
        server.cancel_order(draft["id"], f"Bearer {token_bob}")  # bob 取消 alice 的已取消单
        assert False, "越权取消已取消单应仍被拒（幂等不得绕过归属）"
    except HTTPException as e:
        assert e.status_code == 400
        assert e.detail == "无权取消此订单", "应是归属拒绝，而非幂等成功（否则泄露单存在）"
    print("✅ 步骤B 幂等不绕过归属：别人的已取消单仍被拒")


# ════════════════ 步骤5（后端半）：把 draft_id 透出到事件流 ════════════════
# 前端要拿到 draft_id 才能显示确认按钮。可靠做法：从 place_order 的确定性输出
# （格式 '#N' 我们自己控制）里抠 id，而不是解析 LLM 的自由回复（会被改写，脆）。

def test_extract_draft_id_from_place_order_output():
    out = "已生成待确认订单 #7：airmax 42码 ×1，请在15分钟内确认。订单号：7"
    assert server._extract_draft_id(out) == 7, "应从工具输出抠出 draft_id"
    print("✅ 步骤5 透出：从 place_order 输出抠出 draft_id")


def test_extract_draft_id_none_on_failure():
    assert server._extract_draft_id("下单失败：库存不足: airmax 43") is None, "失败输出无 draft_id"
    assert server._extract_draft_id("没有找到包含「xyz」的商品") is None, "无关输出返回 None"
    print("✅ 步骤5 透出：失败/无关输出 → draft_id 为 None")


def main():
    test_place_order_creates_draft_not_real_order()
    test_place_order_insufficient_stock()
    test_confirm_endpoint_deducts_stock()
    test_confirm_endpoint_rejects_wrong_owner()
    test_confirm_endpoint_requires_auth()
    test_confirm_endpoint_nonexistent()
    test_confirm_endpoint_idempotent()
    test_cancel_endpoint_releases_pending()
    test_cancel_endpoint_rejects_wrong_owner()
    test_cancel_endpoint_requires_auth()
    test_cancel_endpoint_nonexistent()
    test_cancel_endpoint_idempotent()
    test_cancel_idempotent_does_not_bypass_ownership()
    test_extract_draft_id_from_place_order_output()
    test_extract_draft_id_none_on_failure()
    print("\n🎉 步骤2 + 步骤3 + 步骤B + 步骤5后端 全部验证通过")


if __name__ == "__main__":
    main()
