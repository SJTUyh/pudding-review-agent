"""提示词与上下文拼装：把 `skills/*.md` 作为 Agent 的 instructions。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


@lru_cache(maxsize=None)
def load_skill(name: str) -> str:
    """读取 `skills/<name>.md`，文件不存在返回空串。"""
    path = _SKILLS_DIR / f"{name}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def review_instructions() -> str:
    """意图检视的 system instructions。"""
    return load_skill("intent_review")


def build_diff_text(files: list[dict], max_chars: int) -> str:
    """把 PR 变更文件拼成带路径标注的 diff 文本，超限截断。

    截断只是**首轮上下文**的上限；开启工具检索后模型可以再按需拉取完整文件。
    """
    parts: list[str] = []
    total = 0
    truncated = False
    for f in files:
        filename = f.get("filename", "?")
        status = f.get("status", "modified")
        patch = f.get("patch") or "（二进制或无文本 diff）"
        block = f"### 文件: {filename} ({status})\n```diff\n{patch}\n```\n"
        if total + len(block) > max_chars:
            truncated = True
            remain = max_chars - total
            if remain > 200:
                parts.append(block[:remain] + "\n…（此文件被截断）\n")
            break
        parts.append(block)
        total += len(block)
    if truncated:
        parts.append("\n> 注意：diff 过大已被截断，可调用工具读取完整文件后再判断。\n")
    return "\n".join(parts)
