"""强结构化输出模型：交给框架做原生 schema 约束与校验重试，替代正则抠 JSON。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FindingModel(BaseModel):
    """一条行内检视意见。"""

    path: str = Field(description="问题所在文件的仓库相对路径，必须出现在本次 diff 中")
    line: int | None = Field(default=None, description="评论锚定的新文件侧行号（从 1 开始）")
    severity: Literal["critical", "major", "minor", "info"] = Field(
        default="info", description="严重程度"
    )
    body: str = Field(description="中文检视意见：指出问题并给出可执行的修改建议")


class ReviewReport(BaseModel):
    """一次意图检视的完整结果。"""

    summary: str = Field(description="一两句话总结本次检视的结论与整体判断")
    findings: list[FindingModel] = Field(default_factory=list, description="具体问题列表")
