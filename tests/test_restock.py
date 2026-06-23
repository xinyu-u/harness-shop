"""restock_product · 商家增量补货 的数据层 + 工具层验证。

补货语义：qty += add_qty（增量加，绝不是覆写绝对值）。
  - 已有尺码 → 在原库存上加
  - 没有的尺码 → 自动新建该 (product_id, size) 库存行
  - 商品不存在 → 拒绝（store 返回 None / 工具 is_error）
  - 只动 qty 不动 locked（available = qty - locked 仍自洽）
  - 仅商家可调（allowed_roles={"merchant"}）

跑法（项目根目录）：python -m tests.test_restock
"""

import asyncio
from business.store import SqliteStore, MemoryStore
from business.cs_tools import RestockProductTool, build_tools


# ───────── 数据层：增量、新建、拒绝、预占自洽 ─────────

def test_restock_existing_size_increments():
    store = SqliteStore(":memory:")
    assert store.check_stock("airmax", "42") == 5
    new_qty = store.restock("airmax", "42", 20)
    assert new_qty == 25, "增量补货：5+20=25（不是覆写成20）"
    assert store.check_stock("airmax", "42") == 25
    print("✅ 已有尺码增量补货：5→25")


def test_restock_creates_missing_size_row():
    store = SqliteStore(":memory:")
    assert store.check_stock("airmax", "44") == 0, "44码本来没有库存行"
    new_qty = store.restock("airmax", "44", 3)
    assert new_qty == 3
    assert store.check_stock("airmax", "44") == 3, "缺失尺码：自动新建库存行"
    print("✅ 缺失尺码补货：自动新建库存行 →3")


def test_restock_zero_stock_size():
    store = SqliteStore(":memory:")
    assert store.check_stock("airmax", "43") == 0, "43码初始为0（演示无货）"
    store.restock("airmax", "43", 10)
    assert store.check_stock("airmax", "43") == 10, "无货→补货后有货"
    print("✅ 0库存尺码补货：0→10")


def test_restock_unknown_product_rejected():
    store = SqliteStore(":memory:")
    assert store.restock("nope", "42", 5) is None, "不存在的商品补货返回 None"
    print("✅ 不存在商品补货：被拒（None）")


def test_restock_preserves_locked():
    # 补货只动 qty，不动 locked；available = qty - locked
    store = SqliteStore(":memory:")
    store.create_draft_order("airmax", "42", 2, "alice")   # locked=2 → available=3
    assert store.check_stock("airmax", "42") == 3
    store.restock("airmax", "42", 10)                      # qty 5→15
    assert store.check_stock("airmax", "42") == 13, "available = 15 - 2 = 13"
    print("✅ 补货不影响预占：available = qty - locked")


def test_restock_memorystore_parity():
    store = MemoryStore()
    assert store.restock("airmax", "42", 20) == 25
    assert store.restock("airmax", "44", 3) == 3        # 新建尺码
    assert store.restock("nope", "42", 5) is None       # 不存在商品
    print("✅ MemoryStore 补货：与 SqliteStore 行为一致")


# ───────── 工具层：报告新库存、拒绝不存在、角色门禁 ─────────

async def test_restock_tool_reports_new_qty():
    store = SqliteStore(":memory:")
    tool = RestockProductTool(store)
    r = await tool.execute(tool.input_model.model_validate(
        {"product_id": "airmax", "size": "42", "add_qty": 20}))
    assert not r.is_error
    assert "25" in r.output, f"应报新库存25：{r.output}"
    print("✅ 工具补货：报告补货后库存")


async def test_restock_tool_unknown_product_errors():
    store = SqliteStore(":memory:")
    tool = RestockProductTool(store)
    r = await tool.execute(tool.input_model.model_validate(
        {"product_id": "nope", "size": "42", "add_qty": 5}))
    assert r.is_error, "不存在商品应 is_error"
    print("✅ 工具补货：不存在商品报错")


def test_restock_is_merchant_only():
    tool = build_tools(MemoryStore())["restock_product"]
    assert tool.is_write is True, "补货是写操作"
    assert tool.allowed_roles == {"merchant"}, "仅商家可补货"
    print("✅ 补货工具：is_write + 仅商家")


def main():
    test_restock_existing_size_increments()
    test_restock_creates_missing_size_row()
    test_restock_zero_stock_size()
    test_restock_unknown_product_rejected()
    test_restock_preserves_locked()
    test_restock_memorystore_parity()
    asyncio.run(test_restock_tool_reports_new_qty())
    asyncio.run(test_restock_tool_unknown_product_errors())
    test_restock_is_merchant_only()
    print("\n🎉 restock_product 全部验证通过")


if __name__ == "__main__":
    main()
