"""维度：并发安全（确定性套件，阈值 1.0，不烧 API）。

真线程（ThreadPoolExecutor）并发锤【单连接共享 SqliteStore】，复现 server.py:46 的部署形态
（一个连接被所有请求跨线程共享）。断言【安全不变量 + API/DB 一致性】，而非脆弱的精确计数——
单连接跨线程事务串扰下失败是混沌的（超卖 / 丢失更新 / database is locked），写死"恰好N成功"
既脆弱又掩盖病灶。所以：成功口径=调用返回成功；OperationalError 单列诊断桶绝不吞；
不变量从 DB 终态查出来与"返回成功的调用数"对账，对不上即 FAIL。

每个 scenario 独立用 _fresh_sqlite() 起一个共享连接；每条不变量重复 R 轮（并发调度随机，
单跑可能恰好不撞车），任一轮 FAIL 即该条 FAIL。

设计依据：docs/superpowers/specs/2026-06-23-concurrency-eval-design.md
"""

import argparse
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

from eval.harness import _fresh_sqlite, Outcome, CaseResult, print_report

THRESHOLD = 1.0
DEFAULT_ROUNDS = 5


def _raw(store, col, product_id, size):
    """直接读 inventory 表的 qty / locked 原始值（eval 读原始状态，故访问 _conn）。"""
    row = store._conn.execute(
        f"SELECT {col} FROM inventory WHERE product_id=? AND size=?",
        (product_id, size),
    ).fetchone()
    return row[0] if row else None


def _status_qty(store, product_id, size):
    """按 status 聚合该 (product_id,size) 的订单 qty 之和：{status: sum_qty}。"""
    rows = store._conn.execute(
        "SELECT status, COALESCE(SUM(qty), 0) FROM orders "
        "WHERE product_id=? AND size=? GROUP BY status",
        (product_id, size),
    ).fetchall()
    return {s: q for s, q in rows}


def _classify(fn):
    """跑 fn()，把结果归类成诊断桶名。fn 返回真值=成功；ValueError=业务库存不足；
    OperationalError=database is locked（头等症状，绝不吞）；其它=other。"""
    try:
        return "ok" if fn() else "rejected"
    except ValueError:
        return "rejected"
    except sqlite3.OperationalError:
        return "locked"
    except Exception:
        return "other"


def run_concurrency_suite(scenarios, rounds):
    """scenarios: list[(label, fn)]，fn() -> Outcome 且自行打印诊断行。
    每条重复 rounds 轮，任一轮 FAIL 即该条 FAIL。返回 list[CaseResult]。"""
    results = []
    for label, fn in scenarios:
        passes = 0
        for r in range(rounds):
            outcome = fn()
            print(f"  [round {r + 1}/{rounds}] {outcome.value} {label}", flush=True)
            if outcome is Outcome.PASS:
                passes += 1
        results.append(CaseResult(label=label, passes=passes, total=rounds, na=0))
    return results


# ───────────────────────── 场景1：草稿不超卖 ─────────────────────────
def scenario_oversell_draft(n_threads=20) -> Outcome:
    """库存5，N 线程并发 create_draft_order(qty=1)。
    不变量：返回成功的调用数 == pending 行数 == pending qty 之和 == inventory.locked，
    且该值 <= seed（不超卖）；qty 不变（草稿只锁不扣）；qty/locked 非负。"""
    store, path = _fresh_sqlite()
    try:
        seed = _raw(store, "qty", "airmax", "42")   # 5

        def make_worker(i):
            return lambda: store.create_draft_order("airmax", "42", 1, f"u{i}")

        with ThreadPoolExecutor(max_workers=n_threads) as ex:
            tags = list(ex.map(lambda i: _classify(make_worker(i)), range(n_threads)))

        buckets = {k: tags.count(k) for k in ("ok", "rejected", "locked", "other")}
        success = buckets["ok"]
        qty = _raw(store, "qty", "airmax", "42")
        locked = _raw(store, "locked", "airmax", "42")
        agg = _status_qty(store, "airmax", "42")
        pending_qty = agg.get("pending", 0)
        pending_rows = store._conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='pending' "
            "AND product_id='airmax' AND size='42'"
        ).fetchone()[0]

        print(f"    diag oversell: {buckets} | qty={qty} locked={locked} "
              f"pending_rows={pending_rows} pending_qty={pending_qty} seed={seed}")

        ok = (
            success == pending_rows == pending_qty == locked
            and locked <= seed
            and qty == seed
            and locked >= 0 and qty >= 0
        )
        return Outcome.PASS if ok else Outcome.FAIL
    finally:
        store.close()
        try:
            os.unlink(path)
        except OSError:
            pass


SCENARIOS = [
    ("场景1·草稿不超卖（20线程抢库存5）", scenario_oversell_draft),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    args = ap.parse_args()

    results = run_concurrency_suite(SCENARIOS, args.rounds)
    rate = print_report("并发安全（确定性）", results, args.threshold)
    sys.exit(0 if rate >= args.threshold else 1)


if __name__ == "__main__":
    main()
