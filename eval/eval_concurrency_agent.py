"""维度：agent 端到端并发负载冒烟（概率套件；不超卖按 1.0 硬标准）。

为什么用真线程而非 asyncio.gather：agent 对话是 async，但 async 只来自 LLM 网络调用——
工具体里的 SqliteStore 是同步阻塞 sqlite3，不含 await、不让出事件循环。asyncio.gather 下
一次工具调用会跑到底不让出 → DB 操作被事件循环串行化，恰好把要暴露的并发隔离掉（100% 假性通过），
而且不符 server.py（sync 端点跑在线程池，是真线程打共享连接）。所以：ThreadPoolExecutor
一线程一对话，每线程内 asyncio.run 起自己的事件循环、各自新建 client（async client 不能跨
事件循环共享），K 个对话共享同一个 SqliteStore——复现 server.py「线程池 + 单连接共享」。

K 个买家同抢最后 N 件，断言：经 agent 全链路返回成功建成的 pending 草稿数与 DB 实际一致、
且 <= N（不超卖不谎报）；无未捕获异常；locked 非负、qty 未被草稿动过。

EVAL_FAKE=1 走脚本化 FakeClient（不烧 API），仅验证线程/共享 store 的接线跑得通。
设计依据：docs/superpowers/specs/2026-06-23-concurrency-eval-design.md
"""

import argparse
import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from core.messages import ConversationMessage, TextBlock, ToolUseBlock
from eval.harness import _fresh_sqlite, run_case

THRESHOLD = 1.0

# 冒烟脚本：直接发一次 place_order（airmax 42 ×1）再收尾，验证接线不烧 API。
PLACE_FAKE = [
    ConversationMessage(role="assistant", content=[
        ToolUseBlock(name="place_order",
                     input={"product_id": "airmax", "size": "42", "qty": 1})]),
    ConversationMessage(role="assistant", content=[TextBlock(text="已为你下单")]),
]


def _run_one(store, prompt, force_fake):
    """在独立线程里跑一个 agent 对话：自起事件循环 + run_case 自建 client，共享传入的 store。"""
    return asyncio.run(run_case(
        prompt, role="user", store=store,
        fake_script=(PLACE_FAKE if force_fake else None),
        force_fake=force_fake,
    ))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--buyers", type=int, default=10)
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    args = ap.parse_args()

    smoke = os.getenv("EVAL_FAKE") == "1"
    store, path = _fresh_sqlite()
    seed = store._conn.execute(
        "SELECT qty FROM inventory WHERE product_id='airmax' AND size='42'"
    ).fetchone()[0]
    prompt = "我要买最后那双 airmax 42 码"
    traces = []
    try:
        with ThreadPoolExecutor(max_workers=args.buyers) as ex:
            try:
                traces = list(ex.map(lambda _: _run_one(store, prompt, smoke),
                                     range(args.buyers)))
            except Exception as exc:
                print(f"buyer 线程抛异常（并发下 store 可能崩了）："
                      f"{type(exc).__name__}: {exc}")
                raise

        # agent 侧"成功"：place_order 成功执行（executed_ok）
        agent_success = sum(1 for t in traces if t.executed_ok("place_order"))
        # DB 真相
        pending_rows, pending_qty = store._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(qty), 0) FROM orders "
            "WHERE status='pending' AND product_id='airmax' AND size='42'"
        ).fetchone()
        qty = store._conn.execute(
            "SELECT qty FROM inventory WHERE product_id='airmax' AND size='42'"
        ).fetchone()[0]
        locked = store._conn.execute(
            "SELECT locked FROM inventory WHERE product_id='airmax' AND size='42'"
        ).fetchone()[0]

        print(f"\n===== agent 并发负载冒烟（buyers={args.buyers}, seed={seed}）=====")
        print(f"  agent_success={agent_success} pending_rows={pending_rows} "
              f"pending_qty={pending_qty} | qty={qty} locked={locked}")

        ok = (
            agent_success == pending_rows == pending_qty == locked
            and pending_rows <= seed
            and qty == seed           # 草稿只锁不扣，真实库存不变
            and locked >= 0
        )
        if not ok:
            reasons = []
            if not (agent_success == pending_rows == pending_qty == locked):
                reasons.append(f"计数不一致 agent_success={agent_success} "
                               f"pending_rows={pending_rows} pending_qty={pending_qty} "
                               f"locked={locked}")
            if pending_rows > seed:
                reasons.append(f"超卖 pending_rows={pending_rows} > seed={seed}")
            if qty != seed:
                reasons.append(f"qty 被动过 qty={qty} != seed={seed}（草稿应只锁不扣）")
            if locked < 0:
                reasons.append(f"locked 为负 locked={locked}")
            print("  失败原因：" + "；".join(reasons))

        rate = 1.0 if ok else 0.0
        print(f"\n不超卖一致性：{'PASS' if ok else 'FAIL'}  阈值 {args.threshold:.0%}")
        print("结果：" + ("达标" if rate >= args.threshold else "未达标"))
        sys.exit(0 if rate >= args.threshold else 1)
    finally:
        for t in traces:
            t.cleanup()           # 清各自记忆文件；共享 store 不在此关（_owns_store=False）
        store.close()
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
