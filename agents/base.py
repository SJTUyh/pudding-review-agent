"""Agent 层核心接口：请求 / 结果 / 后端协议。

约定：除 `agents/` 之外的任何模块都不允许直接调用模型
（不 import 任何 LLM SDK、不发 `/chat/completions`），一律经由 `agents.registry.get_backend()`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# 与 adapters/github_api.py 的下单口径一致
VALID_SEVERITY = ("critical", "major", "minor", "info")


@dataclass
class Finding:
    """一条检视意见（对应 PR 上的一条行内评论）。"""

    path: str
    line: int | None
    severity: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        sev = self.severity if self.severity in VALID_SEVERITY else "info"
        return {"path": self.path, "line": self.line, "severity": sev, "body": self.body}


@dataclass
class ReviewRequest:
    """一次检视请求的全部输入。"""

    intent: str
    repo: str
    pr_number: int
    pr_title: str
    files: list[dict] = field(default_factory=list)
    extra: str = ""
    # 工具检索用的 ref（一般是 PR head sha），为 None 则用默认分支
    head_sha: str | None = None
    # 检视人自己的 gho_ token，供内置工具访问 GitHub
    github_token: str | None = None


@dataclass
class ReviewResult:
    """一次检视的结构化结果。`to_dict()` 保持既有草稿存储格式。"""

    summary: str
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
        }


@runtime_checkable
class ReviewBackend(Protocol):
    """Agent 后端协议。第三方后端只需实现这三个方法。"""

    name: str

    def available(self) -> tuple[bool, str]:
        """（是否可用，原因）。不可用时 registry 会按降级链继续尝试。"""
        ...

    def probe(self) -> str:
        """连通性自检，返回一句人类可读的描述；失败请抛异常。"""
        ...

    def review(self, req: ReviewRequest) -> ReviewResult:
        """同步执行一次检视。内部可 `asyncio.run`，但不得跨线程复用 event loop。"""
        ...
