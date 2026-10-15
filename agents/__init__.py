"""Agent 层：所有「需要模型处理」的信息都经由这里。

对外只暴露 registry.get_backend()；业务代码不要直接 import 任何 LLM SDK。
"""
from agents.base import Finding, ReviewBackend, ReviewRequest, ReviewResult
from agents.registry import get_backend

__all__ = [
    "Finding",
    "ReviewBackend",
    "ReviewRequest",
    "ReviewResult",
    "get_backend",
]
