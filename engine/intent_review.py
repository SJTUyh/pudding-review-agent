"""意图检视引擎：把一次检视请求转交 Agent 层，本模块不再直接调用模型。

模型调用与结果解析已全部收敛到 `agents/`（见 `agent化改造方案.md`）：
本模块只负责「组装 `ReviewRequest` → 取 `AGENT_BACKEND` 选定的后端 → 转成草稿存储格式」。
"""
from __future__ import annotations

from agents import ReviewRequest, get_backend
from agents.base import ReviewBackend


class IntentReviewEngine:
    """保留既有对外签名；推理由注册在 `agents.registry` 的后端完成。"""

    def __init__(self, backend: ReviewBackend | None = None):
        self.backend = backend or get_backend()

    def review(
        self,
        intent: str,
        repo: str,
        pr_number: int,
        pr_title: str,
        files: list[dict],
        extra: str = "",
        head_sha: str | None = None,
        github_token: str | None = None,
    ) -> dict:
        result = self.backend.review(
            ReviewRequest(
                intent=intent,
                repo=repo,
                pr_number=pr_number,
                pr_title=pr_title,
                files=files,
                extra=extra,
                head_sha=head_sha,
                github_token=github_token,
            )
        )
        return result.to_dict()
