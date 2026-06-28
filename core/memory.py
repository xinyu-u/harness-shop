"""跨会话记忆（keyed 结构 · upsert 覆盖）。

设计原则：
  - 记忆存硬盘文件（跨会话不丢），内存只在用时读。
  - 存储是 keyed 结构 {key: {value, updated_at}}：同一 key 再写就【覆盖】，
    不再 append-only 堆历史——这样「先记42、后改43」只会留下最新的 43，不自相矛盾。
  - 当前范围只锁定一类：尺码（鞋码 key=shoe_size、上衣码 key=top_size）。
    不做品类/订单/交付记忆——两个 SKU 撑不起，加了就是过度设计。
  - 读：会话开始 load_memory，渲染成几行拼进 system_prompt。
  - 写：模型通过 write_memory(key, value) 工具自己决定记什么；已有同 key 就更新。

文件名仍用 memory_<user>.md（沿用现有 cleanup 路径：harness / tests 都按 .md 删），
内部存的是 JSON 字典。
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

# 投毒护栏：模型可写的记忆 key 白名单。范围锁死在尺码两类，其余一律拒绝。
# 这是【代码约束】而非 prompt 指引——system_prompt 拦不住对抗输入，写入路径必须硬拦。
# 注意：append_memory 的 _note_<uuid> 不走这条校验（仅测试用、不暴露给模型）。
ALLOWED_MEMORY_KEYS = frozenset({"shoe_size", "top_size"})


def _path(user_id: str) -> Path:
    return Path(f"memory_{user_id}.md")


def _read(user_id: str) -> dict:
    """读出 keyed 字典（不存在/空/坏格式都返回空字典）。"""
    path = _path(user_id)
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _write(user_id: str, data: dict) -> None:
    _path(user_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def upsert_memory(key: str, value: str, user_id: str = "default") -> None:
    """写入一条记忆：同一 key 覆盖（不再 append 堆历史）。"""
    data = _read(user_id)
    data[key] = {"value": value, "updated_at": datetime.now(timezone.utc).isoformat()}
    _write(user_id, data)


def load_memory(user_id: str = "default") -> str:
    """读记忆，渲染成 markdown 行返回（不存在/空 → 空字符串）。"""
    data = _read(user_id)
    if not data:
        return ""
    return "\n".join(f"- {item['value']}" for item in data.values())


def append_memory(content: str, user_id: str = "default") -> None:
    """兼容旧接口：无 key 的自由记忆，按自动 key 存（各条共存，不互相覆盖）。"""
    upsert_memory(f"_note_{uuid.uuid4().hex[:8]}", content, user_id)
