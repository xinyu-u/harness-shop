"""步骤1 · 预占库存模型（草稿→预占→确认/过期）的数据层验证。

只测 SqliteStore（生产用它）。用 :memory: 库，每次全新、互不污染。

核心五个验证点（mentor 指定）：
  1. 建草稿后 available 减1、但 qty 没真减（只是预占）
  2. 确认后 qty 真减（预占转真扣）
  3. 过期未确认 → 释放预占，available 还原、状态变 cancelled
  4. 越权确认被拒（拿别人的 token 确认别人的草稿）
  5. 重复确认不重复扣（幂等）

跑法（项目根目录）：python -m tests.test_draft_order
"""

from business.store import SqliteStore


def _raw_inventory(store, product_id, size):
    """直接读库存表原始的 (qty, locked)，绕过 check_stock 的 available 计算。"""
    row = store._conn.execute(
        "SELECT qty, locked FROM inventory WHERE product_id = ? AND size = ?",
        (product_id, size),
    ).fetchone()
    return (row[0], row[1])


# ───────── 验证1：建草稿 → 预占（available 减，qty 不减）─────────

def test_create_draft_reserves_not_deducts():
    store = SqliteStore(":memory:")
    qty0, locked0 = _raw_inventory(store, "airmax", "42")
    avail0 = store.check_stock("airmax", "42")
    assert (qty0, locked0, avail0) == (5, 0, 5), "初始：qty=5 locked=0 available=5"

    draft = store.create_draft_order("airmax", "42", 1, "alice")

    assert draft["status"] == "pending", "草稿应是 pending"
    assert draft["expires_at"] is not None, "草稿应有过期时间"
    qty1, locked1 = _raw_inventory(store, "airmax", "42")
    assert qty1 == 5, "建草稿不真扣 qty（qty 仍为 5）"
    assert locked1 == 1, "建草稿预占 1（locked=1）"
    assert store.check_stock("airmax", "42") == 4, "available = qty - locked = 4"
    print("✅ 验证1 建草稿：available 减1、qty 没动、预占 locked+1")


# ───────── 验证1b：预占耗尽后建草稿失败（available 不够）─────────

def test_create_draft_fails_when_no_available():
    store = SqliteStore(":memory:")
    # 把 5 件全预占掉
    for _ in range(5):
        store.create_draft_order("airmax", "42", 1, "alice")
    assert store.check_stock("airmax", "42") == 0, "5 件全预占，available=0"

    try:
        store.create_draft_order("airmax", "42", 1, "bob")
        assert False, "available 为 0 时建草稿应抛 ValueError"
    except ValueError:
        pass
    print("✅ 验证1b 防超卖：available 不足时建草稿被拒")


# ───────── 验证2：确认 → 预占转真扣（qty 真减）─────────

def test_confirm_deducts_qty():
    store = SqliteStore(":memory:")
    draft = store.create_draft_order("airmax", "42", 2, "alice")

    order, err = store.confirm_draft_order(draft["id"], "alice")

    assert err is None, f"确认应成功，却得到错误：{err}"
    assert order["status"] == "confirmed", "确认后状态应为 confirmed"
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert qty == 3, "确认后 qty 真减 2（5→3）"
    assert locked == 0, "确认后预占释放（locked 回 0）"
    assert store.check_stock("airmax", "42") == 3, "available = 3"
    print("✅ 验证2 确认：qty 真减、locked 归还、available 一致")


# ───────── 验证3：过期未确认 → 释放预占、状态 cancelled ─────────

def test_expired_draft_released():
    store = SqliteStore(":memory:")
    # ttl 设负数 → 立即过期。（不能用 check_stock 看预占中间态：它会惰性释放过期草稿）
    draft = store.create_draft_order("airmax", "42", 1, "alice", ttl_seconds=-1)
    assert _raw_inventory(store, "airmax", "42") == (5, 1), "建草稿后 qty=5 locked=1（已预占）"

    released = store.release_expired_orders()

    assert released == 1, "应释放 1 个过期草稿"
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert (qty, locked) == (5, 0), "过期释放后 qty=5 locked=0（还原）"
    assert store.check_stock("airmax", "42") == 5, "available 还原为 5"
    assert store.get_order(draft["id"])["status"] == "cancelled", "过期草稿状态变 cancelled"
    print("✅ 验证3 过期释放：预占归还、available 还原、状态 cancelled")


# ───────── 验证3b：确认一个已过期的草稿 → 被拒 ─────────

def test_confirm_expired_rejected():
    store = SqliteStore(":memory:")
    draft = store.create_draft_order("airmax", "42", 1, "alice", ttl_seconds=-1)

    order, err = store.confirm_draft_order(draft["id"], "alice")

    assert err is not None, "确认过期草稿应被拒"
    assert order is None, "被拒时不返回订单"
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert (qty, locked) == (5, 0), "确认过期草稿不扣库存、预占已释放"
    print("✅ 验证3b 过期确认：被拒、库存不动")


# ───────── 验证4：越权确认 → 被拒 ─────────

def test_confirm_wrong_owner_rejected():
    store = SqliteStore(":memory:")
    draft = store.create_draft_order("airmax", "42", 1, "alice")

    order, err = store.confirm_draft_order(draft["id"], "bob")  # bob 确认 alice 的草稿

    assert err is not None, "越权确认应被拒"
    assert order is None, "被拒时不返回订单"
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert (qty, locked) == (5, 1), "越权确认不扣库存、预占仍在"
    assert store.get_order(draft["id"])["status"] == "pending", "草稿仍是 pending（未被破坏）"
    print("✅ 验证4 越权确认：被拒、草稿不受影响")


# ───────── 验证5：重复确认 → 幂等，不重复扣 ─────────

def test_confirm_idempotent():
    store = SqliteStore(":memory:")
    draft = store.create_draft_order("airmax", "42", 2, "alice")

    order1, err1 = store.confirm_draft_order(draft["id"], "alice")
    order2, err2 = store.confirm_draft_order(draft["id"], "alice")  # 再确认一次

    assert err1 is None and err2 is None, "两次确认都应成功（幂等）"
    assert order2["status"] == "confirmed"
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert qty == 3, "重复确认 qty 只减一次（5→3，不是 1）"
    assert locked == 0, "locked 不会被减成负数"
    print("✅ 验证5 幂等确认：重复确认不重复扣")


# ───────── 验证6：cancel_order 按状态退对地方（pending 退 locked，已扣的退 qty）─────────
# 库存在两个"位置"：pending 占在 locked、created/confirmed 已从 qty 真扣。
# 取消时必须退回正确的位置，否则账目错乱（qty 凭空变多 / locked 不释放）。

def test_cancel_pending_releases_lock_not_qty():
    store = SqliteStore(":memory:")
    draft = store.create_draft_order("airmax", "42", 1, "alice")
    assert _raw_inventory(store, "airmax", "42") == (5, 1), "建草稿：qty=5 locked=1"

    ok = store.cancel_order(draft["id"])

    assert ok is True
    qty, locked = _raw_inventory(store, "airmax", "42")
    assert qty == 5, "取消 pending 草稿不该动 qty（库存没真扣过，别凭空 +1）"
    assert locked == 0, "取消 pending 草稿应释放 locked"
    assert store.get_order(draft["id"])["status"] == "cancelled"
    print("✅ 验证6 取消草稿：释放 locked、qty 不动")


def test_cancel_confirmed_refunds_qty():
    store = SqliteStore(":memory:")
    draft = store.create_draft_order("airmax", "42", 2, "alice")
    store.confirm_draft_order(draft["id"], "alice")     # qty=3 locked=0
    assert _raw_inventory(store, "airmax", "42") == (3, 0)

    ok = store.cancel_order(draft["id"])

    assert ok is True
    assert _raw_inventory(store, "airmax", "42") == (5, 0), "取消 confirmed 应退回 qty"
    print("✅ 验证6 取消已确认单：退回 qty")


def test_cancel_created_refunds_qty():
    # 路径A 直接下单的 created 订单，取消仍退 qty —— 不回归
    store = SqliteStore(":memory:")
    order = store.create_order("airmax", "42", 1, "alice")   # qty=4 locked=0
    assert _raw_inventory(store, "airmax", "42") == (4, 0)

    ok = store.cancel_order(order["id"])

    assert ok is True
    assert _raw_inventory(store, "airmax", "42") == (5, 0), "取消 created（路径A）应退回 qty"
    print("✅ 验证6 取消路径A订单：退回 qty（不回归）")


def test_cancel_pending_releases_lock_memorystore():
    # MemoryStore 同样的 bug、同样要修，保证两实现行为一致
    from business.store import MemoryStore
    store = MemoryStore()
    draft = store.create_draft_order("airmax", "42", 1, "alice")
    assert store._inventory[("airmax", "42")] == 5 and store._locked[("airmax", "42")] == 1

    store.cancel_order(draft["id"])

    assert store._inventory[("airmax", "42")] == 5, "MemoryStore 取消草稿不该动 qty"
    assert store._locked[("airmax", "42")] == 0, "MemoryStore 取消草稿应释放 locked"
    print("✅ 验证6 MemoryStore 取消草稿：与 SqliteStore 行为一致")


# ───────── 验证7：状态机"cancel 行/列"的组合（取消后再操作）─────────
# cancel 接口/按钮即将让用户任意触发取消，这些组合会被真实触发：先用测试钉死。

def test_confirm_after_cancel_rejected():
    """cancel 掉的草稿，再 confirm → 被拒，库存不动。"""
    store = SqliteStore(":memory:")
    draft = store.create_draft_order("airmax", "42", 1, "alice")
    store.cancel_order(draft["id"])                    # pending → cancelled

    order, err = store.confirm_draft_order(draft["id"], "alice")

    assert err is not None and order is None, "已取消的草稿不能再确认"
    assert _raw_inventory(store, "airmax", "42") == (5, 0), "确认被拒，库存不动"
    print("✅ 验证7 取消后确认：被拒、库存不动")


def test_cancel_confirmed_then_cancel_again():
    """confirmed 订单 cancel（退 qty）→ 再 cancel → 第二次 False，不二次退。"""
    store = SqliteStore(":memory:")
    draft = store.create_draft_order("airmax", "42", 2, "alice")
    store.confirm_draft_order(draft["id"], "alice")    # → confirmed, qty 5→3

    assert store.cancel_order(draft["id"]) is True      # confirmed → cancelled, qty 3→5
    assert _raw_inventory(store, "airmax", "42") == (5, 0), "退回 qty"
    assert store.cancel_order(draft["id"]) is False     # 再 cancel → False
    assert _raw_inventory(store, "airmax", "42") == (5, 0), "不二次退"
    print("✅ 验证7 已确认单取消再取消：第二次 False、不二次退 qty")


def test_cancel_then_cancel_pending():
    """pending 草稿 cancel → 再 cancel → 第二次 False，不二次释放 locked。"""
    store = SqliteStore(":memory:")
    draft = store.create_draft_order("airmax", "42", 1, "alice")

    assert store.cancel_order(draft["id"]) is True
    assert store.cancel_order(draft["id"]) is False
    assert _raw_inventory(store, "airmax", "42") == (5, 0), "不二次释放 locked"
    print("✅ 验证7 草稿取消再取消：第二次 False、不二次释放 locked")


def main():
    test_create_draft_reserves_not_deducts()
    test_create_draft_fails_when_no_available()
    test_confirm_deducts_qty()
    test_expired_draft_released()
    test_confirm_expired_rejected()
    test_confirm_wrong_owner_rejected()
    test_confirm_idempotent()
    test_cancel_pending_releases_lock_not_qty()
    test_cancel_confirmed_refunds_qty()
    test_cancel_created_refunds_qty()
    test_cancel_pending_releases_lock_memorystore()
    test_confirm_after_cancel_rejected()
    test_cancel_confirmed_then_cancel_again()
    test_cancel_then_cancel_pending()
    print("\n🎉 步骤1 预占库存模型 全部验证通过")


if __name__ == "__main__":
    main()
