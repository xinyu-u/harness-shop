"""维度1：工具选择准确率。

判定：期望工具出现在 trace.tool_calls（意图）即算对——不要求是第一个调用，
因为模型可能"先查再下单"。expected_tool=None 表示"不该调任何工具"。
阈值 0.8。
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass

from core.messages import ConversationMessage, TextBlock, ToolUseBlock
from eval.harness import Outcome, run_suite, print_report

THRESHOLD = 0.8


@dataclass
class Case:
    prompt: str
    expected_tool: str | None
    role: str = "user"
    fake_script: list | None = None        # 仅冒烟模式用


CASES = [
    Case("有没有 air 的鞋", "search_products",
         fake_script=[
             ConversationMessage(role="assistant", content=[
                 ToolUseBlock(name="search_products", input={"keyword": "air"})]),
             ConversationMessage(role="assistant", content=[TextBlock(text="找到 Air Max")]),
         ]),
    Case("airmax 42码还有几件", "check_stock"),
    Case("我178cm 70kg 穿鞋多少码", "recommend_size"),
    Case("订单1什么状态", "get_order_status"),
    Case("我要买一双 airmax 42", "place_order"),
    Case("取消订单1", "cancel_order"),
    Case("把 airmax 改成500", "update_price", role="merchant"),
    Case("上架一款新卫衣，叫连帽卫衣，价格199", "add_product", role="merchant"),
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
