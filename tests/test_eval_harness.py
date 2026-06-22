"""用 FakeClient 测 run_case：fresh 临时 sqlite、事件采集、ground-truth 帮手。"""

import asyncio

from core.messages import ConversationMessage, TextBlock, ToolUseBlock
from eval.harness import run_case, seeded_store_value


def _script():
    # 第1次回工具调用，第2次回收尾文字（让循环停下来）
    return [
        ConversationMessage(role="assistant", content=[
            ToolUseBlock(name="check_stock", input={"product_id": "airmax", "size": "42"})]),
        ConversationMessage(role="assistant", content=[TextBlock(text="42码有货，库存5件")]),
    ]


def test_run_case_with_fake_client():
    trace = asyncio.run(run_case("airmax 42还有吗", fake_script=_script(), force_fake=True))
    try:
        assert trace.called("check_stock")
        assert trace.executed_ok("check_stock")
        assert "5" in trace.results[0].output       # seed: airmax 42 = 5
        assert "库存5件" in trace.final_text
    finally:
        trace.cleanup()
    print("[PASS] test_run_case_with_fake_client")


def test_seeded_store_value_is_clean_seed():
    price = seeded_store_value(lambda s: s.get_product("airmax")["price"])
    stock = seeded_store_value(lambda s: s.check_stock("airmax", "42"))
    assert price == 899 and stock == 5          # 干净 seed，与外部 shop.db 无关
    print("[PASS] test_seeded_store_value_is_clean_seed")


if __name__ == "__main__":
    test_run_case_with_fake_client()
    test_seeded_store_value_is_clean_seed()
