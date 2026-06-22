"""维度3：任务完成正确性（end-to-end）。

ground-truth 实时从全新 seed 库算出（seeded_store_value），不写死数值。
判定：可接受关键词任一出现在最终答案文本里（关键词匹配，不上 LLM-judge）。
阈值 0.8。
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Callable

from core.messages import ConversationMessage, TextBlock, ToolUseBlock
from eval.harness import Outcome, print_report, run_suite, seeded_store_value

THRESHOLD = 0.8


@dataclass
class Case:
    prompt: str
    expected_fn: Callable
    role: str = "user"
    fake_script: list | None = None


CASES = [
    Case(
        "airmax 42码还有几件",
        lambda s: [str(s.check_stock("airmax", "42"))],
        fake_script=[
            ConversationMessage(role="assistant", content=[
                ToolUseBlock(name="check_stock", input={"product_id": "airmax", "size": "42"}),
            ]),
            ConversationMessage(role="assistant", content=[TextBlock(text="airmax 42码有货，库存5件")]),
        ],
    ),
    Case("airmax 多少钱", lambda s: [str(s.get_product("airmax")["price"])]),
    Case("我178cm 70kg 穿鞋推荐什么码", lambda s: [s.recommend_size(178, 70, "鞋")]),
    Case("airmax 43码有货吗", lambda s: ["无货", "没货", "0", "缺货", "售罄", "不足", "没有", "空了"]),
    Case("有没有 air 相关的商品", lambda s: ["Air Max", "airmax"]),
]


def judge(case: Case, trace) -> Outcome:
    keywords = seeded_store_value(case.expected_fn)
    text = trace.final_text
    return Outcome.PASS if any(kw in text for kw in keywords) else Outcome.FAIL


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    args = ap.parse_args()

    results = asyncio.run(run_suite(CASES, judge, trials=args.trials))
    rate = print_report("任务完成正确性", results, args.threshold, args.trials)
    sys.exit(0 if rate >= args.threshold else 1)


if __name__ == "__main__":
    main()
