"""维度1：工具选择准确率。

判定：期望工具出现在 trace.tool_calls（意图）即算对——不要求是第一个调用，
因为模型可能"先查再下单"。expected_tool=None 表示"不该调任何工具"（闲聊/收尾）。

设计原则（重要）：这是概率性评估，价值在覆盖度与多样性，不在单条复杂度。
  - 每个工具多条不同措辞（书面 / 口语 / 多意图 / 带干扰信息）。
  - 重点投在「易混淆对」：search_products↔check_stock、place_order↔add_product、
    update_price↔add_product——这些是工具描述最易导致误路由处，正是 eval 的价值。
建议跑 `--trials 3` 看一致性。阈值 0.8。
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass

from core.messages import ConversationMessage, TextBlock, ToolUseBlock
from eval.harness import Outcome, print_report, run_suite

THRESHOLD = 0.8


@dataclass
class Case:
    prompt: str
    expected_tool: str | None
    role: str = "user"
    label: str = ""
    fake_script: list | None = None        # 仅冒烟模式用


CASES = [
    # ---- search_products：问「有哪些 / 卖不卖」----
    Case("有没有 air 的鞋", "search_products", label="search-有没有",
         fake_script=[
             ConversationMessage(role="assistant", content=[
                 ToolUseBlock(name="search_products", input={"keyword": "air"})]),
             ConversationMessage(role="assistant", content=[TextBlock(text="找到 Air Max")]),
         ]),
    Case("你们都卖些什么商品", "search_products", label="search-卖什么"),
    Case("有卖 T恤 吗", "search_products", label="search-有卖吗"),
    Case("店里有没有 airmax 这一款", "search_products", label="search-有没有这款"),

    # ---- check_stock：问「某商品某尺码还剩几件」----
    Case("airmax 42码还有几件", "check_stock", label="stock-还有几件"),
    Case("那双 Air 的鞋 42 码还有没有货", "check_stock", label="stock-口语"),
    Case("airmax 43 有货吗", "check_stock", label="stock-有货吗"),
    Case("tshirt L 码库存还剩多少", "check_stock", label="stock-库存多少"),
    # 易混淆 search↔stock：明确到尺码 → check_stock
    Case("airmax 42 还剩几双", "check_stock", label="stock-vs-search"),

    # ---- recommend_size ----
    Case("我178cm 70kg 穿鞋多少码", "recommend_size", label="size-基本"),
    Case("身高170 体重55，上衣推荐什么码", "recommend_size", label="size-上衣"),
    Case("我175 65kg，鞋子该买几码", "recommend_size", label="size-口语"),

    # ---- get_order_status ----
    Case("订单1什么状态", "get_order_status", label="order-状态"),
    Case("帮我看看 999 号订单还在不在", "get_order_status", label="order-不存在"),
    Case("我那个单号5的订单到哪了", "get_order_status", label="order-口语"),

    # ---- place_order：用户购买 ----
    Case("我要买一双 airmax 42", "place_order", label="buy-基本"),
    Case("如果 airmax 42 有货，帮我下单一双", "place_order", label="buy-多步"),
    Case("给我来一件 tshirt L 码", "place_order", label="buy-口语"),
    # 易混淆 place_order↔add_product：用户买 → place_order
    Case("帮我把这双 airmax 42 码拍下来", "place_order", label="buy-vs-add"),

    # ---- cancel_order ----
    Case("取消订单1", "cancel_order", label="cancel-基本"),
    Case("我不想要订单3了，帮我退了吧", "cancel_order", label="cancel-口语"),

    # ---- update_price（仅商家）----
    Case("把 airmax 改成500", "update_price", role="merchant", label="price-基本"),
    Case("airmax 现在卖799了，更新一下价格", "update_price", role="merchant", label="price-口语"),
    # 易混淆 update_price↔add_product：改已存在商品 → update_price
    Case("把 tshirt 的价格调到 89", "update_price", role="merchant", label="price-vs-add"),

    # ---- add_product（仅商家）----
    Case("上架一款新卫衣，叫连帽卫衣，价格199", "add_product", role="merchant", label="add-基本"),
    Case("新增一个商品：运动裤，价格159，品类下装", "add_product", role="merchant", label="add-结构化"),
    # 易混淆 add_product↔update_price / place_order：进新款 → add_product
    Case("我要进一款全新的鞋，叫飞鞋，定价1000", "add_product", role="merchant", label="add-进新款"),

    # ---- None：不该调任何工具（闲聊 / 收尾）----
    Case("谢谢你，先不用了", None, label="none-收尾"),
    Case("你们一般几点关门呀", None, label="none-闲聊"),
]


def judge(case: Case, trace) -> Outcome:
    if case.expected_tool is None:
        return Outcome.PASS if not trace.tool_calls else Outcome.FAIL
    return Outcome.PASS if trace.called(case.expected_tool) else Outcome.FAIL


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    args = ap.parse_args()

    results = asyncio.run(run_suite(CASES, judge, trials=args.trials))
    rate = print_report("工具选择准确率", results, args.threshold, args.trials)
    sys.exit(0 if rate >= args.threshold else 1)


if __name__ == "__main__":
    main()
