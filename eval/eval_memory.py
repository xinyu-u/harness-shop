"""维度：记忆一致性 + 越权隔离（确定性套件，阈值 1.0）。

验证的是【代码机制】，不是模型概率行为：记忆层用 keyed 结构 upsert（core/memory.py:
upsert_memory 同 key 覆盖），同一用户先记 42、后改记 43 时用同一个 key（shoe_size），
旧值必须被覆盖，memory_<user>.md 里只剩最新的 43、不再自相矛盾。这与模型「愿不愿意」
覆盖无关，是机制必然 → 阈值 1.0，任何 FAIL 都是真 bug（曾经的 append-only 基线就挂在这）。

所以这里用 FakeClient 脚本化模型的 write_memory 决策，离线、零 API、稳定必现——
隔离掉「模型是否决定写记忆」的概率性，只考机制本身。

复现「先说42、后说43」用【两次会话】：固定同一 user_id，第 1 个 engine 写 42，
第 2 个全新 engine（模拟重启）写 43，最贴合「跨会话改主意」，与
tests/test_memory.py 的 _layer4_cross_session 跨会话写法一致。

注：run_suite/run_case 是单轮且每 case 用唯一 uuid user，撑不起「两次会话同一 user」，
故本文件自带极小的两轮 driver，但复用 harness 的 Outcome/CaseResult/print_report 做报表，
报表口径与兄弟 eval 文件一致。

未来「概率性」变体（真实模型自行决定是否覆盖旧记忆）可另起一套件按通过率评——
本文件只交付确定性基线。

跑法（项目根目录，无需 API 凭证）：python -m eval.eval_memory
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from core.client import FakeClient
from core.engine import QueryEngine
from core.memory import load_memory
from core.messages import ConversationMessage, TextBlock, ToolUseBlock
from business.store import MemoryStore
from business.cs_tools import build_tools
from eval.harness import CaseResult, Outcome, print_report

THRESHOLD = 1.0

U = "eval_mem_contradiction"          # 固定 user_id：两次会话共享同一记忆文件
MEM_FILE = f"memory_{U}.md"

UA = "eval_mem_user_a"                 # 越权隔离：A 写记忆
UB = "eval_mem_user_b"                 # 越权隔离：B 不应读到 A 的记忆


def _cleanup_user(user_id: str) -> None:
    """删掉某用户的记忆文件（跑前跑后各一次），避免跨次跑串读。"""
    path = Path(f"memory_{user_id}.md")
    if path.exists():
        os.remove(path)


def _cleanup():
    """删掉本套件的记忆文件（跑前跑后各一次），避免跨次跑串读。"""
    _cleanup_user(U)


def _script_write(key: str, value: str, closing: str) -> FakeClient:
    """脚本化一轮：模型先调 write_memory(key, value)，再纯文字收尾。

    write_memory 是 is_write=False，不走 confirm，引擎直接执行。
    """
    return FakeClient(scripted=[
        ConversationMessage(role="assistant", content=[
            ToolUseBlock(name="write_memory", input={"key": key, "value": value})]),
        ConversationMessage(role="assistant", content=[TextBlock(text=closing)]),
    ])


async def _run_contradiction() -> Outcome:
    """两次会话同一 user：先记 42，后改记 43。断言记忆只剩最新事实 43。

    期望不变量：记忆只保留最新事实（43），旧事实（42）应被覆盖/删除。
    append-only 现状下两条都在 → FAIL（基线）。修好后此 case 应转 PASS。
    """
    _cleanup()

    # 会话1：记住 42 码（key=shoe_size）
    engine1 = QueryEngine(
        _script_write("shoe_size", "用户穿42码鞋", "记下了"),
        build_tools(MemoryStore(), user_id=U), user_id=U,
    )
    _ = [ev async for ev in engine1.submit_message("我穿42码")]

    # 会话2：全新 engine（模拟重启），改主意 → 同一个 key 覆盖成 43 码
    engine2 = QueryEngine(
        _script_write("shoe_size", "用户改穿43码鞋", "已更新"),
        build_tools(MemoryStore(), user_id=U), user_id=U,
    )
    _ = [ev async for ev in engine2.submit_message("我改主意了，改穿43码")]

    mem = load_memory(U)
    _cleanup()

    only_latest = ("43码" in mem) and ("42码" not in mem)
    return Outcome.PASS if only_latest else Outcome.FAIL


async def _run_isolation() -> Outcome:
    """越权隔离：A 记了 42 码，B（不同 user_id）的新会话 system_prompt 绝不能带上
    A 的尺码——否则就是跨用户越权读记忆。

    机制必然：记忆按 memory_<user>.md 分文件隔离，load_memory(B) 只读 B 的文件、
    里面没有 A 的数据 → 拼进 B 的 system_prompt 也读不到。与模型概率行为无关，
    任何 FAIL 都是隔离被打穿的真 bug → 阈值 1.0。

    断言落在 system_prompt（注入入口）而非 load_memory 返回值：A 的尺码必须在
    B 真正喂给 client 的 system_prompt 里缺席，才算隔离闭环（FakeClient 记录
    last_system_prompt）。
    """
    _cleanup_user(UA)
    _cleanup_user(UB)

    # A 会话：记住 42 码（key=shoe_size），落在 memory_<UA>.md
    engine_a = QueryEngine(
        _script_write("shoe_size", "用户穿42码鞋", "记下了"),
        build_tools(MemoryStore(), user_id=UA), user_id=UA,
    )
    _ = [ev async for ev in engine_a.submit_message("我穿42码")]

    # B 会话：全新 engine、不同 user_id，纯文字收尾即可（只为捕获 system_prompt）
    fake_b = FakeClient(scripted=[
        ConversationMessage(role="assistant", content=[TextBlock(text="您好")]),
    ])
    engine_b = QueryEngine(
        fake_b, build_tools(MemoryStore(), user_id=UB), user_id=UB,
    )
    _ = [ev async for ev in engine_b.submit_message("推荐个鞋码")]

    prompt_b = fake_b.last_system_prompt
    _cleanup_user(UA)
    _cleanup_user(UB)

    isolated = prompt_b is not None and "42码" not in prompt_b
    return Outcome.PASS if isolated else Outcome.FAIL


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    args = ap.parse_args()

    async def _run_all():
        return await _run_contradiction(), await _run_isolation()

    contradiction, isolation = asyncio.run(_run_all())
    results = [
        CaseResult(
            label="记忆矛盾-改尺码42→43",
            passes=1 if contradiction is Outcome.PASS else 0,
            total=1,
            na=0,
        ),
        CaseResult(
            label="越权隔离-B读不到A的尺码",
            passes=1 if isolation is Outcome.PASS else 0,
            total=1,
            na=0,
        ),
    ]
    rate = print_report("记忆一致性 + 越权隔离（确定性）", results, args.threshold)
    sys.exit(0 if rate >= args.threshold else 1)


if __name__ == "__main__":
    main()
