"""评估 harness：把真实 agent 跑一遍，归约成可观测 Trace；并提供跑批/聚合/报表。

采集口径（关键，勿混用）：
  tool_calls  ← ToolExecutionStarted  ＝模型选了哪个工具（意图），不管之后是否被门禁/确认拦下
  results     ← ToolExecutionCompleted ＝工具真正执行（带 is_error）；"成功执行"＝is_error=False
"""

import asyncio
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.events import (
    AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted,
)
from core.messages import TextBlock
from core.client import OpenAIClient, FakeClient
from core.engine import QueryEngine
from business.store import SqliteStore
from business.cs_tools import build_tools


@dataclass
class ToolCall:
    name: str
    input: dict


@dataclass
class ToolResultRecord:
    name: str
    output: str
    is_error: bool


@dataclass
class Trace:
    prompt: str
    role: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolResultRecord] = field(default_factory=list)
    final_text: str = ""
    store: object = None
    _db_path: str | None = None
    _mem_user: str | None = None   # 本 case 独立记忆命名空间，cleanup 时连同文件删除
    _owns_store: bool = True        # 自建库=True（cleanup 负责关+删）；注入共享库=False（调用方统一关）

    # ---- 给判定函数用的小帮手 ----
    def called(self, name: str) -> bool:
        """模型是否选过这个工具（意图，含被拦下的）。"""
        return any(c.name == name for c in self.tool_calls)

    def executed_ok(self, name: str) -> bool:
        """这个工具是否成功执行过（is_error=False）。"""
        return any(r.name == name and not r.is_error for r in self.results)

    def any_executed_ok(self, names) -> bool:
        """names 里任一工具是否成功执行过。"""
        return any(self.executed_ok(n) for n in names)

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


def reduce_events(events, prompt: str, role: str, store, db_path: str | None = None) -> Trace:
    """把事件列表归约成 Trace。纯函数，不碰引擎/IO。"""
    trace = Trace(prompt=prompt, role=role, store=store, _db_path=db_path)
    texts: list[str] = []
    for e in events:
        if isinstance(e, ToolExecutionStarted):
            trace.tool_calls.append(ToolCall(e.tool_name, e.tool_input))
        elif isinstance(e, ToolExecutionCompleted):
            trace.results.append(ToolResultRecord(e.tool_name, e.output, e.is_error))
        elif isinstance(e, AssistantTurnComplete):
            texts.append("".join(b.text for b in e.message.content if isinstance(b, TextBlock)))
    trace.final_text = "\n".join(t for t in texts if t)
    return trace


def _fresh_sqlite():
    """建一个全新的临时 sqlite 文件并返回 (store, path)。_init_db 自动塞 seed。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)                       # 空文件即可，SqliteStore 打开后建表+seed
    return SqliteStore(path), path


def seeded_store_value(fn):
    """在一个全新 seed 库上算 fn(store) 的值（用于实时 ground-truth），算完即清理。"""
    store, path = _fresh_sqlite()
    try:
        return fn(store)
    finally:
        store.close()
        try:
            os.unlink(path)
        except OSError:
            pass


def make_client(fake_script=None, force_fake: bool = False):
    """force_fake 或 EVAL_FAKE=1 → 脚本化 FakeClient；否则真实 OpenAIClient。"""
    if force_fake or os.getenv("EVAL_FAKE") == "1":
        return FakeClient(scripted=fake_script)
    return OpenAIClient()


async def run_case(prompt, role="user",
                   client=None, fake_script=None, force_fake=False, setup=None,
                   store=None) -> Trace:
    """在一个全新临时 sqlite 上跑真实对话循环，返回 Trace（store 仍打开，供判定函数读）。

    复现前端 web 流程：confirm=None——写操作不经 CLI 式确认回调，
    安全完全由「角色门禁（schema 过滤 + 执行兜底）」+「草稿状态机（place_order 只锁不扣，
    真扣只在独立后端 confirm 接口）」保证。所以这里不测 is_write 的确认拦截。

    setup(store)：可选钩子，在跑对话【前】往全新 seed 库里预置状态（如另一用户的订单），
    用来测跨用户越权——本 case 的对话以唯一 mem_user 身份跑，setup 种的别人数据归属不同，
    归属校验该把它挡在外面。无 setup 时行为不变。

    注意：不在这里清理 db——safety 判定要在 store 打开时读状态机；清理由 run_suite 在判定后调
    trace.cleanup()。
    """
    if store is None:
        store, path = _fresh_sqlite()   # 自建临时库，本 case 拥有它
        owns_store = True
    else:
        path = None                     # 注入的共享库：不在 cleanup 里关/删
        owns_store = False
    if setup is not None:
        setup(store)                       # 预置"别人的"数据，再以本 case 身份跑
    if client is None:
        client = make_client(fake_script, force_fake)

    # 每个 case 独立记忆命名空间：write_memory 默认写共享的 memory_default.md，
    # 会跨 case 串读（每次 submit 都把记忆拼进 system_prompt）、并发下还会文件追加竞争。
    # 给唯一 user_id 让读写各走各的 memory_eval_<uuid>.md，随 case 一起清理。
    mem_user = f"eval_{uuid.uuid4().hex}"
    engine = QueryEngine(
        client, build_tools(store, user_id=mem_user), role=role, user_id=mem_user,
    )
    events = [e async for e in engine.submit_message(prompt)]
    trace = reduce_events(events, prompt, role, store, db_path=path)
    trace._mem_user = mem_user
    trace._owns_store = owns_store
    return trace


class Outcome(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NA = "NA"        # 前提未触发，不计分（如：状态机不变量但模型没下单）


@dataclass
class CaseResult:
    label: str
    passes: int      # trials 里 PASS 次数
    total: int       # 计分次数（PASS+FAIL，排除 NA）
    na: int          # NA 次数


def summarize(results):
    """聚合：返回 (rate, passes, total, na)。total 不含 NA；无可计分项时 rate=1.0。"""
    passes = sum(r.passes for r in results)
    total = sum(r.total for r in results)
    na = sum(r.na for r in results)
    rate = passes / total if total else 1.0
    return rate, passes, total, na


def _active_cases(cases):
    """冒烟模式下只保留可脚本化的 case。"""
    smoke = os.getenv("EVAL_FAKE") == "1"
    active = []
    for case in cases:
        if smoke and not getattr(case, "force_fake", False) and getattr(case, "fake_script", None) is None:
            continue
        active.append(case)
    return active


def _resolve_concurrency(concurrency: int | None) -> int:
    """并发上限：显式参数 > EVAL_CONCURRENCY 环境变量 > 默认 8。下限 1。

    上限受你的 API 配额（RPM/并发）约束——撞限流时 engine 会把错误吞成无 tool_calls 的
    文本回复，导致 case 静默 FAIL，所以默认保守，按需经 EVAL_CONCURRENCY 调高/调低。
    """
    if concurrency is None:
        raw = os.getenv("EVAL_CONCURRENCY")
        concurrency = int(raw) if raw else 8
    return max(1, concurrency)


async def run_suite(cases, judge, *, trials: int = 1, concurrency: int | None = None):
    """跑一批 case，返回 list[CaseResult]。case 之间互相独立、并发跑（受 concurrency 限流）。

    case 鸭子类型需有：.prompt .role；可选 .label .fake_script .force_fake。
    judge(case, trace) -> Outcome。
    EVAL_FAKE=1 且 case 无 fake_script/force_fake → 跳过（冒烟模式只跑可脚本化的）。

    client 复用：真实模型全套件共享一个 OpenAIClient（复用连接池，省 TLS 握手）。
    FakeClient 有可变状态（call_count/scripted 索引），绝不能共享——force_fake/冒烟
    路径传 client=None，由 run_case 每 case 现造。
    """
    active = _active_cases(cases)
    total_runs = len(active) * trials
    smoke = os.getenv("EVAL_FAKE") == "1"
    # 真实模型共享 client；冒烟模式不建（无需 API 凭证）。
    shared_client = None if smoke else make_client()

    def client_for(case):
        if smoke or getattr(case, "force_fake", False):
            return None        # 现造 FakeClient(scripted=fake_script)
        return shared_client

    sem = asyncio.Semaphore(_resolve_concurrency(concurrency))
    done = 0   # 完成计数（asyncio 单线程，run_one 内自增→打印之间无 await，天然原子）

    async def run_one(ci: int, case):
        nonlocal done
        async with sem:                      # 只限并发 run_case（API 往返），judge 很快不占名额
            trace = await run_case(
                case.prompt,
                role=getattr(case, "role", "user"),
                client=client_for(case),
                fake_script=getattr(case, "fake_script", None),
                force_fake=getattr(case, "force_fake", False),
                setup=getattr(case, "setup", None),
            )
        try:
            outcome = judge(case, trace)
        finally:
            trace.cleanup()
        done += 1
        tools = ",".join(c.name for c in trace.tool_calls) or "-"
        label = (getattr(case, "label", None) or case.prompt)[:40]
        print(f"[{done}/{total_runs}] {outcome.value} {tools} {label}", flush=True)
        return ci, outcome

    tasks = [run_one(ci, case) for ci, case in enumerate(active) for _ in range(trials)]
    outcomes = await asyncio.gather(*tasks)

    # 按 case 聚合（gather 返回顺序不定，靠 ci 归位）
    agg = [{"passes": 0, "total": 0, "na": 0} for _ in active]
    for ci, outcome in outcomes:
        if outcome is Outcome.NA:
            agg[ci]["na"] += 1
        else:
            agg[ci]["total"] += 1
            if outcome is Outcome.PASS:
                agg[ci]["passes"] += 1

    return [
        CaseResult(
            label=(getattr(case, "label", None) or case.prompt)[:40],
            passes=agg[ci]["passes"], total=agg[ci]["total"], na=agg[ci]["na"],
        )
        for ci, case in enumerate(active)
    ]


def print_report(title, results, threshold, trials=1) -> float:
    """打印逐 case 表 + 汇总，返回总 rate。"""
    print(f"\n===== {title} =====")
    for r in results:
        if r.total == 0 and r.na > 0:
            mark, detail = "N/A", f"N/A x{r.na}"
        else:
            ok = r.passes == r.total
            mark = "PASS" if ok else "FAIL"
            detail = f"{r.passes}/{r.total}" + (f" (N/A {r.na})" if r.na else "")
        print(f"  {mark:>4} [{detail:>12}] {r.label}")
    rate, passes, total, na = summarize(results)
    print(f"\n通过率 {rate:.0%}  ({passes}/{total} 计分, {na} 条 N/A)  阈值 {threshold:.0%}")
    print("结果：" + ("达标" if rate >= threshold else "未达标"))
    return rate
