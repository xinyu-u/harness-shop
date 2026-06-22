"""维度3：任务完成正确性（end-to-end）。

ground-truth 实时从全新 seed 库算出（seeded_store_value），不写死数值——
即便尺码表/库存改了，期望值也跟着变，且与 agent 读的同一份 seed 自洽。

设计原则：只选【答案唯一、可精确匹配】的问题（数值 / 固定关键词），不选开放问答
（那需要 LLM-judge）。边界值比复杂 case 更能抓 bug：尺码表边界、库存=0、搜索命中。
判定：可接受关键词任一出现在最终答案文本里。建议 `--trials 3`。阈值 0.8。
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
    expected_fn: Callable          # (store) -> list[str] 可接受关键词
    role: str = "user"
    label: str = ""
    fake_script: list | None = None


# 缺货的合理表述（收紧：不要裸 "0" / 裸 "没有"，避免无关文本误判 PASS）
OUT_OF_STOCK = ["无货", "没货", "缺货", "售罄", "暂时没有", "目前没有", "0 件", "0件", "没有货"]


CASES = [
    # 查库存：有货（精确数值）
    Case("airmax 42码还有几件",
         lambda s: [str(s.check_stock("airmax", "42"))],      # 5
         label="库存-有货",
         fake_script=[
             ConversationMessage(role="assistant", content=[
                 ToolUseBlock(name="check_stock", input={"product_id": "airmax", "size": "42"})]),
             ConversationMessage(role="assistant", content=[TextBlock(text="airmax 42码有货，库存5件")]),
         ]),
    Case("tshirt L码还有多少件",
         lambda s: [str(s.check_stock("tshirt", "L"))],       # 10
         label="库存-另一商品"),
    # 查库存：无货（边界 qty=0）
    Case("airmax 43码有货吗",
         lambda s: OUT_OF_STOCK,                              # check_stock=0
         label="库存-无货边界"),
    # 查价格
    Case("airmax 多少钱",
         lambda s: [str(s.get_product("airmax")["price"])],   # 899
         label="价格-airmax"),
    Case("tshirt 一件多少钱",
         lambda s: [str(s.get_product("tshirt")["price"])],   # 99
         label="价格-tshirt"),
    # 推荐尺码：普通 + 边界（178 落 43 不落 42；ground-truth 实时算，自洽）
    Case("我178cm 70kg 穿鞋推荐什么码",
         lambda s: [s.recommend_size(178, 70, "鞋")],          # 43（边界）
         label="尺码-边界178"),
    Case("我170cm 55kg 穿鞋推荐什么码",
         lambda s: [s.recommend_size(170, 55, "鞋")],          # 42
         label="尺码-普通"),
    Case("身高160 体重50 上衣推荐什么码",
         lambda s: [s.recommend_size(160, 50, "上衣")],        # M
         label="尺码-上衣"),
    # 搜索命中（用 air 而非品类"鞋"——search 只按名称/ID 匹配）
    Case("有没有 air 相关的商品",
         lambda s: ["Air Max", "airmax"],
         label="搜索-命中"),
]


def judge(case: Case, trace) -> Outcome:
    keywords = seeded_store_value(case.expected_fn)   # 在干净 seed 库上实时算 ground-truth
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
