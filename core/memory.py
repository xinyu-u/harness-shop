"""跨会话记忆（阶段6 实现，极简版）。

设计原则（你已理解的）：
  - 记忆存硬盘文件（跨会话不丢），内存只在用时读
  - 记忆是模型概括的精华（一句话），不是原始对话
  - 读：会话开始 read，拼进 system_prompt
  - 写：模型通过 write_memory 工具自己决定记什么（模型概括，你只存）

明确不做：检索打分(search)、使用统计(usage)、元数据(schema)、TTL、团队记忆
（记忆就几条，全读即可，那些是大规模记忆才需要的，是过度设计）

参考：OpenHarness memory/memdir.py（读+注入）、memory/manager.py（写）
     —— 只取读写核心，别碰那一堆管理功能。
"""

from pathlib import Path


def load_memory(user_id: str = "default") -> str:
    """TODO 阶段6：读记忆文件，返回字符串（不存在返回空）。"""
    path = Path(f"memory_{user_id}.md")
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def append_memory(content: str, user_id: str = "default") -> None:
    """TODO 阶段6：把一条记忆要点追加进文件。"""
    with open(f"memory_{user_id}.md", "a", encoding="utf-8") as f:
        f.write(f"\n- {content}")

# TODO 阶段6：写一个 WriteMemoryTool（business/cs_tools.py 里），
#            让模型自己决定调它、自己概括内容，工具调 append_memory 存。
