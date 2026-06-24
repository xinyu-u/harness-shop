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


# ───────────────────────── 场景2：确认幂等（只扣一次）─────────────────────────
def scenario_confirm_idempotent(n_threads=20) -> Outcome:
    """一张 pending 草稿，N 线程并发 confirm_draft_order。
    不变量：qty 只扣一次（== seed-1，不是 seed-N）；locked 只释放一次（== 0，不为负）；
    终态 status=='confirmed'。双重确认会把 qty 扣两次/locked 扣成负——本断言能抓到。"""
    store, path = _fresh_sqlite()
    try:
        seed = _raw(store, "qty", "airmax", "42")
        did = store.create_draft_order("airmax", "42", 1, "alice")["id"]

        def confirm():
            order, err = store.confirm_draft_order(did, "alice")
            return err is None and order is not None

        with ThreadPoolExecutor(max_workers=n_threads) as ex:
            tags = list(ex.map(lambda _: _classify(confirm), range(n_threads)))

        buckets = {k: tags.count(k) for k in ("ok", "rejected", "locked", "other")}
        qty = _raw(store, "qty", "airmax", "42")
        locked = _raw(store, "locked", "airmax", "42")
        status = store.get_order(did)["status"]

        print(f"    diag confirm-idem: {buckets} | qty={qty} locked={locked} "
              f"status={status} seed={seed}")

        ok = (qty == seed - 1 and locked == 0 and status == "confirmed")
        return Outcome.PASS if ok else Outcome.FAIL
    finally:
        store.close()
        try:
            os.unlink(path)
        except OSError:
            pass


# ───────────────────────── 场景3：confirm vs cancel 竞态 ─────────────────────────
def scenario_confirm_vs_cancel() -> Outcome:
    """同一草稿，confirm 与 cancel 并发。判定用【库存完整性】——它天然把
    "合法的确认后退款"（confirm 成功后 cancel 退回，终态 cancelled、qty 复原、locked=0）
    与"竞态损坏"（两者都读到 pending → confirm 扣完、cancel 又按 pending 分支把 locked 减成负）
    区分开：损坏一定表现为 locked!=0 或 qty 越界。
    不变量：locked==0；status∈{confirmed,cancelled}；
            confirmed → qty==seed-1（扣一次保留）；cancelled → qty==seed（净零，没扣或扣后退）。"""
    store, path = _fresh_sqlite()
    try:
        seed = _raw(store, "qty", "airmax", "42")
        did = store.create_draft_order("airmax", "42", 1, "alice")["id"]

        def do_confirm():
            order, err = store.confirm_draft_order(did, "alice")
            return ("confirm", err is None and order is not None)

        def do_cancel():
            return ("cancel", store.cancel_order(did))

        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(do_confirm), ex.submit(do_cancel)]
            outs = {}
            for f in futs:
                try:
                    k, v = f.result()
                    outs[k] = v
                except Exception as exc:
                    outs[str(exc)[:30]] = False

        qty = _raw(store, "qty", "airmax", "42")
        locked = _raw(store, "locked", "airmax", "42")
        status = store.get_order(did)["status"]

        print(f"    diag confirm-vs-cancel: {outs} | qty={qty} locked={locked} "
              f"status={status} seed={seed}")

        ok = (
            locked == 0
            and status in ("confirmed", "cancelled")
            and ((status == "confirmed" and qty == seed - 1)
                 or (status == "cancelled" and qty == seed))
        )
        return Outcome.PASS if ok else Outcome.FAIL
    finally:
        store.close()
        try:
            os.unlink(path)
        except OSError:
            pass


# ───────────────────────── 场景4：混合并发·账本对账 ─────────────────────────
def scenario_mixed_ledger(n=12) -> Outcome:
    """先补足库存（避免被超卖瓶颈干扰，本场景测的是账本一致性），并发建 n 张草稿，
    再并发对每张草稿 confirm（偶数）/ cancel（奇数）。
    不变量（任何合法串行化都应成立）：
        inventory.locked == 所有 pending 订单 qty 之和
        inventory.qty    == base - 所有 confirmed 订单 qty 之和
        locked>=0、qty>=0、qty-locked>=0"""
    store, path = _fresh_sqlite()
    try:
        store.restock("airmax", "42", 100)               # 库存充足，专测对账
        base = _raw(store, "qty", "airmax", "42")         # seed + 100

        def mk(i):
            try:
                return store.create_draft_order("airmax", "42", 1, "alice")["id"]
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=n) as ex:
            ids = [oid for oid in ex.map(mk, range(n)) if oid is not None]

        def act(pair):
            idx, oid = pair
            try:
                if idx % 2 == 0:
                    store.confirm_draft_order(oid, "alice")
                else:
                    store.cancel_order(oid)
            except Exception:
                pass

        if ids:
            with ThreadPoolExecutor(max_workers=len(ids)) as ex:
                list(ex.map(act, list(enumerate(ids))))

        agg = _status_qty(store, "airmax", "42")
        pending_qty = agg.get("pending", 0)
        confirmed_qty = agg.get("confirmed", 0)
        qty = _raw(store, "qty", "airmax", "42")
        locked = _raw(store, "locked", "airmax", "42")

        print(f"    diag mixed-ledger: drafts={len(ids)} agg={agg} | "
              f"qty={qty} locked={locked} base={base}")

        ok = (
            locked == pending_qty
            and qty == base - confirmed_qty
            and locked >= 0 and qty >= 0 and (qty - locked) >= 0
        )
        return Outcome.PASS if ok else Outcome.FAIL
    finally:
        store.close()
        try:
            os.unlink(path)
        except OSError:
            pass


# ───────────────────────── 场景5：restock 原子自增 ─────────────────────────
def scenario_restock_atomic(n=20) -> Outcome:
    """N 线程并发 restock(add_qty=1)。
    不变量：末态 qty == seed + 返回成功的次数（每次成功恰好 +1）。
    丢失更新（某线程的 +1 被别的线程 commit/rollback 抹掉）会让 qty < seed + 成功数 → FAIL。
    无锁冲突时成功数应为 N，qty == seed + N。"""
    store, path = _fresh_sqlite()
    try:
        seed = _raw(store, "qty", "airmax", "42")

        def bump():
            store.restock("airmax", "42", 1)
            return True

        with ThreadPoolExecutor(max_workers=n) as ex:
            tags = list(ex.map(lambda _: _classify(bump), range(n)))

        buckets = {k: tags.count(k) for k in ("ok", "rejected", "locked", "other")}
        qty = _raw(store, "qty", "airmax", "42")

        print(f"    diag restock: {buckets} | qty={qty} seed={seed} "
              f"expected={seed + buckets['ok']}")

        ok = (qty == seed + buckets["ok"])
        return Outcome.PASS if ok else Outcome.FAIL
    finally:
        store.close()
        try:
            os.unlink(path)
        except OSError:
            pass


SCENARIOS = [
    ("场景1·草稿不超卖（20线程抢库存5）", scenario_oversell_draft),
    ("场景2·确认幂等只扣一次（20线程确认同一草稿）", scenario_confirm_idempotent),
    ("场景3·confirm vs cancel 竞态（库存完整性）", scenario_confirm_vs_cancel),
    ("场景4·混合并发账本对账（建/确认/取消）", scenario_mixed_ledger),
    ("场景5·restock 原子自增（20线程并发补货）", scenario_restock_atomic),
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
