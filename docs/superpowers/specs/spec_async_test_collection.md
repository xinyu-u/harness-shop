# 实施 Spec：让 9 个 async 测试在 pytest 下真实运行（同步包法 + live skipif）

> 给 Claude Code 的任务说明。请**按文件顺序**实施，每改完一个文件用 pytest 自检该文件。
> **先读完「不可违反的约束」再动手。** 这是一个纯测试基建改动，不碰任何业务代码。

---

## 1. 目标

`python -m pytest tests/` 当前是 `73 passed, 9 skipped`。那 9 条 skip 全是裸 `async def test_`
函数——仓库装了 `pytest-asyncio` 但没开自动模式，这些函数又没有 `@pytest.mark.asyncio`，
pytest 跑不了协程，于是静默跳过（`async def function and no async plugin installed`）。

「静默跳过」是测试里最危险的状态：看着「绿」，其实有 9 条根本没跑。本次把它们收口：

- **7 条非 live**（FakeClient / 本地，无网络）→ 改成**同步包法**，pytest 真实执行。
- **2 条 live**（真打 OpenAI API）→ 同样改同步包法，并加 `@pytest.mark.skipif`：
  无 `api_key` 时**显式按条件跳过**（带清楚的 reason），有 key 时才真跑。

9 条的归属（来自 `grep -n '^async def test_' tests/`）：

| 文件 | 函数 | 类型 |
|---|---|---|
| `tests/test_engine.py` | `test_check_stock_flow` `test_max_turns` | 非 live（FakeClient） |
| `tests/test_memory.py` | `test_layer2_memory_in_system_prompt` `test_layer3_write_memory_tool` `test_layer4_cross_session` | 非 live（FakeClient） |
| `tests/test_restock.py` | `test_restock_tool_reports_new_qty` `test_restock_tool_unknown_product_errors` | 非 live（SqliteStore） |
| `tests/test_openai_client_live.py` | `test_plain_reply` `test_tool_call_intent` | **live（真 API）** |

---

## 2. 不可违反的约束（GUARDRAILS — 违反即任务失败）

1. **只改「如何被 pytest 收集/执行」，测试体逐字不动。** 断言、`print`、FakeClient 脚本、
   构造的对象——一字不改。本次不是重构测试逻辑，只是换一层执行壳。

2. **保留每个文件的 `if __name__ == "__main__"` 脚本式入口。** 仓库习惯
   `python -m tests.test_engine` 这样单跑，两种跑法都要继续可用。

3. **live 测试默认不联网。** 无 `api_key` 时 `test_openai_client_live` 的两条**必须跳过**，
   绝不能因为换了同步包法就在离线 / CI 里真发 HTTP 请求。这是本次引入 skipif 的全部理由。

4. **不给仓库新增 pytest 配置文件、不改依赖清单。** 同步包法零配置、零新依赖（见 §3）。

5. **不动其它已经在跑的同步测试**（`test_layer1_memory_rw`、`test_restock_*` 的同步那批、
   令牌幂等套件等）。它们与本次无关。

6. **任何一条测试在转换后变红 → 停止并报告，不得修改测试体使其变绿。** 红测试要么是
   测试过时（断言旧行为，超出本次范围，单列报告）、要么是真 bug（更要报告）——本次只换
   执行壳，无权改「测什么」。变红 = 暴露了一个本来被静默 skip 藏住的问题，**这是收益不是
   障碍**。尤其 `test_engine.py` 两条是引擎循环的回归保护，若它们红，先怀疑是不是别处的
   改动碰坏了引擎，而不是去顺手改测试。

---

## 3. 设计决策（背景，供理解，不要重新发明）

- **为什么用「同步包法」而不是开 `asyncio_mode=auto`**：auto 模式要给仓库新增
  `pytest.ini` / `pyproject.toml`、改变全局收集语义、且把「测试能否跑」绑死在
  `pytest-asyncio` 这个未在依赖里声明的插件上（当前 conda 环境恰好有，换环境就裂）。
  同步包法零配置、零依赖，且每条测试**既能 `python -m tests.xxx` 脚本跑、又能被 pytest 收**，
  与仓库现有的 `if __name__ == "__main__": asyncio.run(...)` 习惯、以及已合入的
  `test_token_idempotency.py` / `test_history.py` 的写法一致。

- **为什么 live 用 skipif 而不是删掉 / 标 xfail**：这两条是有价值的真·冒烟（验证真模型会
  按预期回话、会调工具），有 key 时该跑；只是不该在无 key 的离线环境里失败或联网。
  `skipif(not api_key)` 把「隐式的 async 跳过」换成「显式的、带原因的条件跳过」——
  无 key 跳过、配了 `.env` 的 `api_key` 就自动启用，意图写在 reason 里。

- **转换用「改名 + 同步壳」而非「整体内缩进到嵌套 async」**：把原 `async def test_x` 改名为
  `async def _x`（下划线前缀，pytest 不再收集它），再加一个两行同步壳
  `def test_x(): asyncio.run(_x())`。**测试体一行不缩进、不改动**，diff 最小、最易审。

- **与仓库已有写法的一致性**：本范式（同步 `def test_` 调 `asyncio.run(协程)`）与已合入的
  `test_history.py`（`asyncio.run(_drive_stream(...))`，模块级 async helper）、
  `test_token_idempotency.py`（内嵌 `_run()`）是**同一族**——都是「同步测试函数驱动一个协程」。
  三者只在「协程放哪」上有微差（改名 helper / 内嵌），原则完全一致，不构成风格分裂；
  本 spec 取「改名 helper」是因为它对**已存在的测试体**改动最小（不必整体缩进）。

---

## 4. 转换范式（统一照此套）

**通用：把一条裸 async 测试改成同步包法**
```python
# 改前
async def test_x():
    <测试体>

# 改后：原函数改名加下划线前缀（不再被 pytest 收集），加一个同步壳
async def _x():
    <测试体——逐字不动、不改缩进>

def test_x():
    asyncio.run(_x())
```

**对应地，把脚本式入口里的 `asyncio.run(test_x())` 改成直接调同步壳 `test_x()`**
（同步壳内部已经自带 `asyncio.run`，再 `asyncio.run(test_x())` 会因「壳返回 None、
且不能在已运行的 loop 里再 run」而出错）。

> `asyncio` 这几个文件都已 import，同步壳直接用即可，无需新 import（live 文件除外，见 §5.4）。

**同步壳只「调用」、不「return」。** 写成 `asyncio.run(_x())` 单独一行，**不要**写
`return asyncio.run(_x())`。否则会把 `_x()` 的返回值透出去——尤其 live 两条测试体里有
`return elapsed`，壳一旦 `return` 就让测试函数返回非 None，新版 pytest 会对「测试返回非
None」告警。壳不 return → 测试返回 None → 干净。

---

## 4.5 前置检查（agent 动手前先做）

1. **确认 7 个目标测试体里没有嵌套 `asyncio.run`。** 同步壳 `def test_x(): asyncio.run(_x())`
   的前提是 `_x()` 内部不再调 `asyncio.run`（`asyncio.run` 不能在已运行的 loop 里再调，
   会抛 `RuntimeError`）。一行成本排掉一类隐藏崩溃：
   ```bash
   grep -n "asyncio.run" tests/test_engine.py tests/test_memory.py tests/test_restock.py tests/test_openai_client_live.py
   ```
   只允许出现在 `main()` / `if __name__ == "__main__"` 里（那是 runner，本 spec 会改它）。
   **若出现在某个 `async def test_` 的函数体内 → 停下，那条要单独想包法。**
   （本 spec 编写时已跑过此 grep：4 个文件里 `asyncio.run` 全在 runner，测试体内零嵌套——清白。）

2. **先确认这 7 条「现在」就是绿的**（它们久未在 CI 真跑，可能已与代码漂移）。脚本式各跑一遍，
   GBK 控制台会卡在 `✅` 的打印上，用 UTF-8 stdout 跑：
   ```bash
   PYTHONIOENCODING=utf-8 python -m tests.test_engine     # 最高优先级：回归我的引擎改动
   PYTHONIOENCODING=utf-8 python -m tests.test_memory
   PYTHONIOENCODING=utf-8 python -m tests.test_restock
   ```
   > **本 spec 编写时已代跑，结果记录在此**：三者全绿。其中 `test_engine` 两条
   > （`test_check_stock_flow`、`test_max_turns`）通过，**确认本次令牌幂等的引擎改动
   > （`submit_message` 加 `request_token` + ContextVar）未碰坏引擎循环**。
   > 即：交付时这 7 条是真绿，agent 只换执行壳应当一路绿；若 agent 跑出红，按 guardrail 6 停。

---

## 5. 改动清单（按文件，逐个改完逐个自检）

### 5.1 `tests/test_engine.py`（2 条）

- `test_check_stock_flow` → 改名 `_check_stock_flow`，加同步壳 `def test_check_stock_flow()`。
- `test_max_turns` → 改名 `_max_turns`，加同步壳 `def test_max_turns()`。
- 文件末 `if __name__ == "__main__":` 内：
  ```python
  # 改前
  asyncio.run(test_check_stock_flow())
  asyncio.run(test_max_turns())
  # 改后
  test_check_stock_flow()
  test_max_turns()
  ```

**自检**：`python -m pytest tests/test_engine.py -v`（应 2 passed，无 skip）
+ `python -m tests.test_engine`（脚本式仍跑通）。

### 5.2 `tests/test_memory.py`（3 条）

- `test_layer2_memory_in_system_prompt` / `test_layer3_write_memory_tool` /
  `test_layer4_cross_session` → 各自改名加 `_` 前缀 + 同步壳。
- `test_layer1_memory_rw` 是同步的，**不动**。
- `main()` 内：
  ```python
  # 改前
  test_layer1_memory_rw()
  asyncio.run(test_layer2_memory_in_system_prompt())
  asyncio.run(test_layer3_write_memory_tool())
  asyncio.run(test_layer4_cross_session())
  cleanup()
  # 改后
  test_layer1_memory_rw()
  test_layer2_memory_in_system_prompt()
  test_layer3_write_memory_tool()
  test_layer4_cross_session()
  cleanup()
  ```

> **附注（不强求处理）**：这些测试每条开头 `cleanup()` 删 `memory_test_user.md`，但整批的最终
> `cleanup()` 只在 `main()` 里。pytest 单独收集 layer4 时不会跑 `main()`，可能在工作目录留下
> 一个 `memory_test_user.md`。本次可不处理（每条开头的 `cleanup()` 已保证测试间互不污染）；
> 若想顺手干净，可在 `_layer4_cross_session()` 结尾加一行 `cleanup()`。

**自检**：`python -m pytest tests/test_memory.py -v`（应 4 passed）+ `python -m tests.test_memory`。

### 5.3 `tests/test_restock.py`（2 条）

- `test_restock_tool_reports_new_qty` / `test_restock_tool_unknown_product_errors`
  → 各自改名加 `_` 前缀 + 同步壳。其余同步测试**不动**。
- `main()` 内：
  ```python
  # 改前
  asyncio.run(test_restock_tool_reports_new_qty())
  asyncio.run(test_restock_tool_unknown_product_errors())
  # 改后
  test_restock_tool_reports_new_qty()
  test_restock_tool_unknown_product_errors()
  ```

**自检**：`python -m pytest tests/test_restock.py -v`（应 9 passed）+ `python -m tests.test_restock`。

### 5.4 `tests/test_openai_client_live.py`（2 条 + skipif）

- 顶部加 `import pytest`（`asyncio` / `os` 已在）。
- `test_plain_reply` / `test_tool_call_intent` → 改名 `_plain_reply` / `_tool_call_intent`
  （测试体逐字不动，含原来的 `return elapsed`——同步壳不返回它，pytest 不会有「返回非 None」告警）。
- 加两个**带 skipif 的同步壳**：
  ```python
  _NO_KEY = not os.getenv("api_key")
  _SKIP_REASON = "live API 测试：需 .env 配 api_key，离线/CI 默认跳过"

  @pytest.mark.skipif(_NO_KEY, reason=_SKIP_REASON)
  def test_plain_reply():
      asyncio.run(_plain_reply())

  @pytest.mark.skipif(_NO_KEY, reason=_SKIP_REASON)
  def test_tool_call_intent():
      asyncio.run(_tool_call_intent())
  ```
  > skipif 在收集期求值。本模块顶部 `from core.client import OpenAIClient` 会触发
  > `core/client.py` 里的 `load_dotenv()`，故 `os.getenv("api_key")` 此时已从 `.env` 读到——
  > 配了 key 就跑、没配就跳。
- `main()` 当前是 `async def` 且 `await test_plain_reply()`——改成**同步** `def main()`，
  直接调同步壳（它们各自内部 `asyncio.run`，不能在 `main` 的运行 loop 里再被 await/run）：
  ```python
  # 改前
  async def main():
      print(f"timeout={_api_timeout()}s base_url={os.getenv('OPENAI_BASE_URL')}")
      await test_plain_reply()
      await test_tool_call_intent()
  if __name__ == "__main__":
      asyncio.run(main())
  # 改后
  def main():
      print(f"timeout={_api_timeout()}s base_url={os.getenv('OPENAI_BASE_URL')}")
      test_plain_reply()
      test_tool_call_intent()
  if __name__ == "__main__":
      main()
  ```

**自检**：`python -m pytest tests/test_openai_client_live.py -v`
（无 key：应 2 skipped，reason 清楚；有 key：2 passed，会联网）。

---

## 6. 验收命令

```bash
python -m pytest tests/ -q          # 见下方预期
python -m tests.test_engine         # 四个脚本式入口都仍跑通
python -m tests.test_memory
python -m tests.test_restock
python -m tests.test_openai_client_live   # 无 key 时打印后无 live 断言即结束
```

**预期（离线 / 无 `api_key`）**：从 `73 passed, 9 skipped` 变为约 **`80 passed, 2 skipped`**——
7 条非 live 转为真实通过，2 条 live 按 skipif 跳过；**不再出现
「async def function and no async plugin installed」那条警告**。
**若 `.env` 配了 `api_key`**：2 条 live 会真跑（需联网），变 `82 passed, 0 skipped`。

---

## 7. 完成定义（DoD）

1. `pytest tests/` 不再有「async not supported」的 9 条隐式 skip。
2. 7 条非 live async 测试在 pytest 下真实执行并通过（FakeClient / 本地，无网络）。
3. 2 条 live 测试用 `skipif(not api_key)` 显式收口：无 key 带 reason 跳过、有 key 运行。
4. 四个文件的脚本式跑法 `python -m tests.xxx` 仍可用。
5. 测试体逐字未改（约束 1）；既有同步测试 + 令牌幂等套件不受影响、全绿。

---

## 8. 不在本次范围（别顺手做）

- 引入 `pytest-asyncio` auto-mode / 新增任何 pytest 配置文件（见 §3，本次明确不做）。
- 重构测试断言或逻辑——只改「如何被收集执行」，不改「测什么」。
- `memory_test_user.md` 残留文件的彻底清理（§5.2 附注，可选）。
- `tests/test_db.py`——已在前一支单独修过，与本次无关。
- 给 live 测试加 mock / 录制回放——那是另一类工作，不混入本次。
