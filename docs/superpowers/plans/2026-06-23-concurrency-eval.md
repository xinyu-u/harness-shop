# 高并发评估 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `eval/` 加一个并发安全维度：确定性数据层套件（真线程锤单连接 `SqliteStore`，断言并发不变量）+ agent 端到端负载冒烟（线程池一线程一对话、共享 store）。

**Architecture:** 复用 `harness.py` 的 `_fresh_sqlite / Outcome / CaseResult / print_report`。确定性套件 `eval_concurrency.py` 自带小 runner（不走 agent、不烧 API）；agent 冒烟 `eval_concurrency_agent.py` 用 `ThreadPoolExecutor` 一线程一对话（每线程自起事件循环 + 自建 client）共享一个 store，复现 `server.py` 的"线程池 + 单连接"模型。唯一 harness 改动：`run_case` 加 `store=` 注入参数。

**Tech Stack:** Python 3、`concurrent.futures.ThreadPoolExecutor`、`asyncio`、`sqlite3`、现有 `eval.harness`。

**设计依据：** 见 `docs/superpowers/specs/2026-06-23-concurrency-eval-design.md`。核心：断言"安全不变量 + API/DB 一致性"而非脆弱的精确计数；不吞 `OperationalError`，单列诊断桶；路径A/request_id 不纳入。

---

## File Structure

- `eval/harness.py`（改）：`run_case` 加可选 `store=`；`Trace` 加 `_owns_store`；`cleanup()` 共享 store 时不关连接。
- `eval/eval_concurrency.py`（新）：确定性并发套件——5 个 scenario 函数 + 小 runner + `main()`。
- `eval/eval_concurrency_agent.py`（新）：agent 负载冒烟——线程池驱动 K 个对话共享 store。
- `tests/test_eval_shared_store.py`（新）：验证 `run_case(store=...)` 共享语义（不烧 API，用 FakeClient）。
- `eval/README.md`（改）：维度表加两行 + 跑法 + 评估发现占位。

---

### Task 1: harness 支持注入共享 store

**Files:**
- Modify: `eval/harness.py`（`Trace` 数据类、`run_case`）
- Test: `tests/test_eval_shared_store.py`

- [ ] **Step 1: Write the failing test**

`tests/test_eval_shared_store.py`：

```python
"""run_case(store=...) 共享语义：传入的 store 被复用，且 cleanup 不关闭它（由调用方统一关）。"""
import asyncio

from core.messages import ConversationMessage, TextBlock
from eval.harness import run_case, _fresh_sqlite


# 无工具调用的最简脚本：单轮文本回复即结束（避免烧 API）
FAKE = [ConversationMessage(role="assistant", content=[TextBlock(text="你好")])]


def test_injected_store_is_reused_and_not_closed_on_cleanup():
    store, path = _fresh_sqlite()
    try:
        trace = asyncio.run(run_case(
            "在吗", store=store, fake_script=FAKE, force_fake=True,
        ))
        # 复用了同一个 store 实例
        assert trace.store is store
        # cleanup 不应关闭共享 store：cleanup 后仍可查询
        trace.cleanup()
        assert store.check_stock("airmax", "42") == 5
    finally:
        store.close()
        import os
        os.unlink(path)


def test_default_store_still_isolated_and_self_cleaning():
    # 不传 store：行为不变，自建临时库并自行清理
    trace = asyncio.run(run_case("在吗", fake_script=FAKE, force_fake=True))
    assert trace.store is not None
    assert trace._owns_store is True
    trace.cleanup()  # 不抛即可（自己关自己删）


if __name__ == "__main__":
    test_injected_store_is_reused_and_not_closed_on_cleanup()
    test_default_store_still_isolated_and_self_cleaning()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_eval_shared_store.py -v`
Expected: FAIL — `run_case()` 还不接受 `store=` 关键字（`TypeError: unexpected keyword argument 'store'`），且 `Trace` 没有 `_owns_store`。

- [ ] **Step 3: Add `_owns_store` to Trace and guard cleanup**

在 `eval/harness.py` 的 `Trace` 数据类里，`_mem_user` 字段后加一行：

```python
    _mem_user: str | None = None   # 本 case 独立记忆命名空间，cleanup 时连同文件删除
    _owns_store: bool = True        # 自建库=True（cleanup 负责关+删）；注入共享库=False（调用方统一关）
```

把 `cleanup` 的关闭逻辑改成只关自己拥有的 store（`_db_path` 守卫已能挡住共享库的 unlink，因为共享时 `_db_path=None`）：

```python
    def cleanup(self):
        """关闭并删除本 case 的临时 db。判定函数跑完后由 run_suite 调用。

        注入的共享 store（_owns_store=False）不在这里关——由调用方统一关闭。
        """
        import os
        if self._owns_store and self.store is not None:
            self.store.close()
        if self._db_path:
            try:
                os.unlink(self._db_path)
            except OSError:
                pass
        if self._mem_user:
            try:
                os.unlink(f"memory_{self._mem_user}.md")
            except OSError:
                pass   # 没触发 write_memory 就没这文件，正常
```

- [ ] **Step 4: Add `store=` param to run_case**

把 `run_case` 签名末尾加 `store=None`，并把库的获取改成"传入则复用、否则自建"：

```python
async def run_case(prompt, role="user",
                   client=None, fake_script=None, force_fake=False, setup=None,
                   store=None) -> Trace:
```

函数体里把原来的：

```python
    store, path = _fresh_sqlite()
    if setup is not None:
```

替换为：

```python
    if store is None:
        store, path = _fresh_sqlite()   # 自建临时库，本 case 拥有它
        owns_store = True
    else:
        path = None                     # 注入的共享库：不在 cleanup 里关/删
        owns_store = False
    if setup is not None:
```

再把结尾的：

```python
    trace = reduce_events(events, prompt, role, store, db_path=path)
    trace._mem_user = mem_user
    return trace
```

替换为：

```python
    trace = reduce_events(events, prompt, role, store, db_path=path)
    trace._mem_user = mem_user
    trace._owns_store = owns_store
    return trace
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_eval_shared_store.py -v`
Expected: PASS（2 passed）

- [ ] **Step 6: Verify existing suites still wire up (smoke, no API)**

Run: `EVAL_FAKE=1 python -m eval.eval_safety_invariants`
Expected: 正常跑完冒烟 case、打印通过率（验证 harness 改动没破坏现有套件）。

- [ ] **Step 7: Commit**

```bash
git add eval/harness.py tests/test_eval_shared_store.py
git commit -m "eval/harness: run_case 支持注入共享 store（并发评估用）"
```

---

### Task 2: eval_concurrency.py 骨架 + 场景1（草稿不超卖）

**Files:**
- Create: `eval/eval_concurrency.py`

- [ ] **Step 1: Write the file with shared helpers, the runner, scenario 1, and main**

`eval/eval_concurrency.py`：

```python
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
```

- [ ] **Step 2: Run it**

Run: `python -m eval.eval_concurrency --rounds 5`
Expected: 跑完 5 轮、每轮打印 `diag oversell: ...` + PASS/FAIL，末尾打印通过率与达标判定。**若 FAIL 属预期**（单连接事务串扰是 `store.py` 的真实并发缺陷，见 spec"预期：可能跑出真 FAIL"）——此处只验证脚本能跑通并产出诊断；是否修 `store.py` 是独立后续。

- [ ] **Step 3: Commit**

```bash
git add eval/eval_concurrency.py
git commit -m "eval: 并发安全套件骨架 + 场景1（草稿不超卖）"
```

---

### Task 3: 场景2（确认幂等：只扣一次）

**Files:**
- Modify: `eval/eval_concurrency.py`

- [ ] **Step 1: Add scenario function**

在 `scenario_oversell_draft` 之后、`SCENARIOS` 之前加：

```python
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
```

把 `SCENARIOS` 改成：

```python
SCENARIOS = [
    ("场景1·草稿不超卖（20线程抢库存5）", scenario_oversell_draft),
    ("场景2·确认幂等只扣一次（20线程确认同一草稿）", scenario_confirm_idempotent),
]
```

- [ ] **Step 2: Run it**

Run: `python -m eval.eval_concurrency --rounds 5`
Expected: 两条 scenario 各跑 5 轮，打印 `diag confirm-idem: ...`。FAIL 可能是真 bug（见 spec）。

- [ ] **Step 3: Commit**

```bash
git add eval/eval_concurrency.py
git commit -m "eval: 并发场景2（确认幂等只扣一次）"
```

---

### Task 4: 场景3（confirm vs cancel 竞态）

**Files:**
- Modify: `eval/eval_concurrency.py`

- [ ] **Step 1: Add scenario function**

在场景2 之后、`SCENARIOS` 之前加：

```python
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
            outs = dict(f.result() for f in futs)

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
```

`SCENARIOS` 追加一行：

```python
    ("场景3·confirm vs cancel 竞态（库存完整性）", scenario_confirm_vs_cancel),
```

- [ ] **Step 2: Run it**

Run: `python -m eval.eval_concurrency --rounds 5`
Expected: 三条 scenario 各跑 5 轮，打印 `diag confirm-vs-cancel: {'confirm': ..., 'cancel': ...} ...`。

- [ ] **Step 3: Commit**

```bash
git add eval/eval_concurrency.py
git commit -m "eval: 并发场景3（confirm vs cancel 库存完整性）"
```

---

### Task 5: 场景4（混合并发·账本对账）

**Files:**
- Modify: `eval/eval_concurrency.py`

- [ ] **Step 1: Add scenario function**

在场景3 之后、`SCENARIOS` 之前加：

```python
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
```

`SCENARIOS` 追加：

```python
    ("场景4·混合并发账本对账（建/确认/取消）", scenario_mixed_ledger),
```

- [ ] **Step 2: Run it**

Run: `python -m eval.eval_concurrency --rounds 5`
Expected: 四条 scenario，打印 `diag mixed-ledger: drafts=... agg={...} ...`。

- [ ] **Step 3: Commit**

```bash
git add eval/eval_concurrency.py
git commit -m "eval: 并发场景4（混合并发账本对账）"
```

---

### Task 6: 场景5（restock 原子自增·无丢失更新）

**Files:**
- Modify: `eval/eval_concurrency.py`

- [ ] **Step 1: Add scenario function**

在场景4 之后、`SCENARIOS` 之前加：

```python
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
```

`SCENARIOS` 追加：

```python
    ("场景5·restock 原子自增（20线程并发补货）", scenario_restock_atomic),
```

- [ ] **Step 2: Run it**

Run: `python -m eval.eval_concurrency --rounds 5`
Expected: 五条 scenario 全跑，打印 `diag restock: ...` 及总通过率。

- [ ] **Step 3: Commit**

```bash
git add eval/eval_concurrency.py
git commit -m "eval: 并发场景5（restock 原子自增）"
```

---

### Task 7: eval_concurrency_agent.py（agent 负载冒烟）

**Files:**
- Create: `eval/eval_concurrency_agent.py`

- [ ] **Step 1: Write the file**

`eval/eval_concurrency_agent.py`：

```python
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
            traces = list(ex.map(lambda _: _run_one(store, prompt, smoke),
                                 range(args.buyers)))

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
```

- [ ] **Step 2: Run the smoke path (no API)**

Run: `EVAL_FAKE=1 python -m eval.eval_concurrency_agent --buyers 10`
Expected: 10 个线程各跑一遍脚本化 place_order，打印一致性汇总。因 seed=5、买家10，正确实现下 `pending_rows<=5`。验证线程 + 共享 store 接线跑得通。

- [ ] **Step 3: Commit**

```bash
git add eval/eval_concurrency_agent.py
git commit -m "eval: agent 端到端并发负载冒烟（线程池+共享 store）"
```

---

### Task 8: 更新 eval/README.md

**Files:**
- Modify: `eval/README.md`

- [ ] **Step 1: 维度表加两行**

在 `eval/README.md` 的维度表（`| 维度 | 脚本 | 性质 | 测什么 | 阈值 |` 那张）末尾、`任务完成正确性` 行之后追加：

```markdown
| 安全·数据层并发 | `eval_concurrency.py` | 确定 | 真线程锤单连接 SqliteStore：不超卖、确认幂等、confirm/cancel 完整性、账本对账、restock 原子 | 1.0 |
| 安全·agent 并发负载 | `eval_concurrency_agent.py` | 概率 | K 买家共享 store 同抢最后库存 → 经 agent 全链不超卖不谎报 | 1.0 |
```

- [ ] **Step 2: 跑法加命令**

在 `## 跑` 一节、`EVAL_FAKE=1` 冒烟那段之前追加：

```markdown
# 并发安全（确定性数据层，不烧 API；并发调度随机，多跑几轮更稳）
python -m eval.eval_concurrency --rounds 5

# agent 并发负载冒烟（烧 API）；先用 EVAL_FAKE 验证接线
EVAL_FAKE=1 python -m eval.eval_concurrency_agent --buyers 10
python -m eval.eval_concurrency_agent --buyers 10
```

- [ ] **Step 3: 加一条评估发现占位**

在 `## 评估发现（决策留档）` 一节末尾追加：

```markdown
### 2026-06-23 · 并发安全套件（待填实测）

`eval_concurrency.py` 复现 server.py 的「单连接跨线程共享」并断言并发不变量
（不超卖 / 确认幂等 / confirm-cancel 完整性 / 账本对账 / restock 原子）。

> 单连接在事务边界上本就不安全（一个 commit/rollback 作用于整个连接，会波及其它线程
> 未提交的写），所以本套件**可能在现有 store.py 上跑出真实 FAIL**——那是 eval 在干活。
> 修复（每请求一连接 / 序列化写锁）是独立后续，不在本 eval 范围。

**实测结果（待补）：** `python -m eval.eval_concurrency --rounds 5` 的逐场景 PASS/FAIL +
诊断桶（success / rejected / locked / other）贴这里。
```

- [ ] **Step 4: Commit**

```bash
git add eval/README.md
git commit -m "eval/README: 登记并发安全两个新维度 + 跑法 + 发现占位"
```

---

## 实测收尾（全部任务后）

- [ ] 跑确定性套件并记录：`python -m eval.eval_concurrency --rounds 5`
- [ ] 跑 agent 冒烟接线：`EVAL_FAKE=1 python -m eval.eval_concurrency_agent --buyers 10`
- [ ] 把确定性套件的逐场景结果 + 诊断桶回填到 `eval/README.md` 的"评估发现"占位
- [ ] 若出现真实 FAIL：按 `systematic-debugging` 单独立项处理 `store.py`，不在本计划范围内修
```
