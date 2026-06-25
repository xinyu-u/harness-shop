"""尺码完整性：枚举尺码 + 区分"无此尺码/0库存" + 补货不静默建幻觉尺码。

回归背景（boss 人工测试暴露的两个 bug）：
  1. 问"查询尺码和库存"时模型幻觉出 S/M/L/XL、28-32 等不存在的尺码。
     根因：没有"枚举某商品有哪些尺码"的工具，模型只能猜。
  2. check_stock 把"无此尺码"和"该尺码0库存"都返回 0 → 假尺码被洗成"无货"。
  3. restock 对任意尺码静默建行 → 幻觉尺码被写进库存（jeans 28-32 就是这么来的）。

修复：
  A. Store.list_inventory(product_id)：一次列出真实尺码 + 可用量。
  C. check_stock 工具用 list_inventory 区分"无此尺码"和"有此尺码但缺货"。
  B. restock(..., create_size=False)：默认拒绝未知尺码（抛 UnknownSizeError），
     仅在显式确认（create_size=True）时才新建。

跑法（项目根目录）：python -m pytest tests/test_size_integrity.py
"""

import asyncio
import pytest

from business.store import SqliteStore, MemoryStore, UnknownSizeError
from business.cs_tools import CheckStockTool, ListStockTool, RestockProductTool


# ───────── Fix A：list_inventory 枚举真实尺码 ─────────

def test_list_inventory_returns_only_real_sizes():
    store = SqliteStore(":memory:")
    rows = store.list_inventory("airmax")
    sizes = {r["size"] for r in rows}
    assert sizes == {"42", "43"}, f"airmax 真实尺码只有 42/43：{sizes}"
    by_size = {r["size"]: r for r in rows}
    assert by_size["42"]["available"] == 5
    assert by_size["43"]["available"] == 0


def test_list_inventory_unknown_product_returns_none():
    store = SqliteStore(":memory:")
    assert store.list_inventory("nope") is None


def test_list_inventory_memorystore_parity():
    store = MemoryStore()
    sizes = {r["size"] for r in store.list_inventory("airmax")}
    assert sizes == {"42", "43"}
    assert store.list_inventory("nope") is None


def test_list_inventory_reflects_locked():
    store = SqliteStore(":memory:")
    store.create_draft_order("airmax", "42", 2, "alice")   # locked=2
    by_size = {r["size"]: r for r in store.list_inventory("airmax")}
    assert by_size["42"]["available"] == 3, "available = qty - locked = 5 - 2"


# ───────── Fix C：check_stock 工具区分"无此尺码"和"0库存" ─────────

def _run(coro):
    return asyncio.run(coro)


def test_check_stock_tool_reports_unknown_size():
    store = SqliteStore(":memory:")
    tool = CheckStockTool(store)
    # tshirt 只有 L，问 S 应明确"没有这个尺码"，而不是"无货"
    r = _run(tool.execute(tool.input_model.model_validate(
        {"product_id": "tshirt", "size": "S"})))
    assert "没有" in r.output and "S" in r.output, f"应说明无此尺码：{r.output}"
    assert "L" in r.output, f"应列出真实尺码 L：{r.output}"
    assert "无货" not in r.output, f"无此尺码不应措辞为无货：{r.output}"


def test_check_stock_tool_existing_size_zero_stock_says_out_of_stock():
    store = SqliteStore(":memory:")
    tool = CheckStockTool(store)
    # airmax 43 确实存在但库存 0 → 应是"无货"，不是"没有这个尺码"
    r = _run(tool.execute(tool.input_model.model_validate(
        {"product_id": "airmax", "size": "43"})))
    assert "无货" in r.output, f"存在但0库存应说无货：{r.output}"
    assert "没有" not in r.output, f"不应说没有这个尺码：{r.output}"


def test_check_stock_tool_in_stock():
    store = SqliteStore(":memory:")
    tool = CheckStockTool(store)
    r = _run(tool.execute(tool.input_model.model_validate(
        {"product_id": "airmax", "size": "42"})))
    assert "5" in r.output and not r.is_error


# ───────── Fix A：list_stock 工具 ─────────

def test_list_stock_tool_lists_all_sizes():
    store = SqliteStore(":memory:")
    tool = ListStockTool(store)
    r = _run(tool.execute(tool.input_model.model_validate({"product_id": "airmax"})))
    assert not r.is_error
    assert "42" in r.output and "43" in r.output, f"应列出所有真实尺码：{r.output}"


def test_list_stock_tool_unknown_product_errors():
    store = SqliteStore(":memory:")
    tool = ListStockTool(store)
    r = _run(tool.execute(tool.input_model.model_validate({"product_id": "nope"})))
    assert r.is_error


# ───────── Fix B：restock 默认拒绝未知尺码 ─────────

def test_restock_unknown_size_rejected_by_default():
    store = SqliteStore(":memory:")
    with pytest.raises(UnknownSizeError):
        store.restock("airmax", "99", 10)   # 99 码不存在，默认不建
    # 且确实没有把它写进库存
    assert store.list_inventory("airmax") is not None
    assert "99" not in {r["size"] for r in store.list_inventory("airmax")}


def test_restock_unknown_size_created_with_explicit_flag():
    store = SqliteStore(":memory:")
    new_qty = store.restock("airmax", "99", 10, create_size=True)
    assert new_qty == 10
    assert "99" in {r["size"] for r in store.list_inventory("airmax")}


def test_restock_existing_size_still_increments():
    store = SqliteStore(":memory:")
    assert store.restock("airmax", "42", 20) == 25, "已有尺码：增量不受影响"


def test_restock_unknown_product_still_none():
    store = SqliteStore(":memory:")
    assert store.restock("nope", "42", 5) is None


def test_restock_memorystore_parity_unknown_size():
    store = MemoryStore()
    with pytest.raises(UnknownSizeError):
        store.restock("airmax", "99", 10)
    assert store.restock("airmax", "99", 10, create_size=True) == 10


# ───────── Fix B：补货工具对未知尺码报错并列出真实尺码 ─────────

def test_restock_tool_unknown_size_reports_existing_sizes():
    store = SqliteStore(":memory:")
    tool = RestockProductTool(store)
    r = _run(tool.execute(tool.input_model.model_validate(
        {"product_id": "airmax", "size": "99", "add_qty": 10})))
    assert r.is_error, "未知尺码补货应报错而非静默建行"
    assert "42" in r.output or "43" in r.output, f"应提示真实尺码：{r.output}"
    assert "99" not in {row["size"] for row in store.list_inventory("airmax")}


def test_restock_tool_confirm_new_size_creates():
    store = SqliteStore(":memory:")
    tool = RestockProductTool(store)
    r = _run(tool.execute(tool.input_model.model_validate(
        {"product_id": "airmax", "size": "99", "add_qty": 10, "confirm_new_size": True})))
    assert not r.is_error
    assert "99" in {row["size"] for row in store.list_inventory("airmax")}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
