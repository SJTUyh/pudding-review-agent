"""离线后端：不依赖任何模型 SDK，用于自检、单测与最后一层兜底。

它**不会**真的检视代码，只会把请求回显出来并明确告警，
避免「后端不可用」被误读成「没发现问题」。
"""
from __future__ import annotations

from agents.base import ReviewRequest, ReviewResult


class EchoBackend:
    name = "echo"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def probe(self) -> str:
        return "echo 后端（离线，不调用模型）"

    def review(self, req: ReviewRequest) -> ReviewResult:
        summary = (
            f"[未执行模型检视] 当前后端为 echo（离线兜底），"
            f"收到的意图：{req.intent.strip() or '（空）'}；"
            f"变更文件 {len(req.files)} 个。请检查 AGENT_BACKEND 配置与依赖安装。"
        )
        return ReviewResult(summary=summary, findings=[])
