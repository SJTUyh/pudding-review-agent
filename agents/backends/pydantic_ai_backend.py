"""首个 Agent 后端：Pydantic AI（MIT 许可）。

职责：把一次 `ReviewRequest` 变成一次 agent 运行，返回结构化 `ReviewResult`。

三处关键设计：

1. `output_type=ReviewReport` —— 交给框架做**原生 schema 约束 + 校验重试**，
   取代过去用正则从模型输出里抠 JSON 的兜底逻辑。
2. 显式注入 `httpx2.AsyncClient` —— Pydantic AI 自身 HTTP 走**操作系统信任库**，
   不读 `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE`；自签 CA（代理中间人）必须在这里显式 `verify=`。
3. 失败降级 —— 网关若不支持工具调用 / `tool` 结构化输出，第二次改为
   「无工具 + `prompted` 输出模式」重试，prompt 与结果结构完全不变。
"""
from __future__ import annotations

import asyncio
import os

import httpx2
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from agents.base import Finding, ReviewRequest, ReviewResult
from agents.prompts import build_diff_text, review_instructions
from agents.schemas import ReviewReport
from agents.tools import RepoContext, build_tools
from agents.tools.mcp_client import build_mcp_toolsets
from config import Config

# 降级档：让模型把 JSON 直接写在文本里（不依赖网关的 tool calling）
_PROMPTED_PROFILE = {"default_structured_output_mode": "prompted"}


class PydanticAiBackend:
    """基于 Pydantic AI 的检视后端。"""

    name = "pydantic_ai"

    # ---------------- ReviewBackend 协议 ----------------

    def available(self) -> tuple[bool, str]:
        if not Config.llm_api_base:
            return False, "未配置 LLM_BASE_URL"
        if not Config.llm_api_key:
            return False, "未配置 LLM_API_KEY"
        return True, ""

    def probe(self) -> str:
        """一次最简对话，验证「网关可达 + 凭证有效 + CA 可信」。"""

        async def _run() -> None:
            client = self._http_client(timeout=15)
            try:
                agent = self._build_agent(
                    http_client=client,
                    output_type=str,
                    instructions="你是连通性探针，只回复 OK。",
                )
                await agent.run("回复 OK")
            finally:
                await client.aclose()

        asyncio.run(_run())
        return f"model={Config.llm_model}"

    def review(self, req: ReviewRequest) -> ReviewResult:
        prompt = self._build_prompt(req)
        instructions = review_instructions()
        deps, tools = self._prepare_tools(req)
        toolsets = build_mcp_toolsets()
        try:
            report = asyncio.run(
                self._run_agent(
                    prompt, instructions, profile=None, deps=deps, tools=tools, toolsets=toolsets
                )
            )
        except Exception as exc:  # noqa: BLE001 - 网关兼容性差异都归到降级路径
            print(
                "[agents] pydantic_ai 首次检视失败，降级为「无工具 + prompted 纯文本 JSON」重试："
                f"{type(exc).__name__}: {exc}"
            )
            report = asyncio.run(
                self._run_agent(
                    prompt, instructions, profile=_PROMPTED_PROFILE, deps=None, tools=[]
                )
            )

        findings = [
            Finding(path=f.path, line=f.line, severity=f.severity, body=f.body)
            for f in report.findings
            if f.path.strip() and f.body.strip()
        ]
        return ReviewResult(
            summary=report.summary.strip() or "（模型未返回总结）",
            findings=findings,
        )

    # ---------------- 内部实现 ----------------

    @staticmethod
    def _http_client(timeout: float) -> httpx2.AsyncClient:
        """自建客户端：显式信任 CA bundle，并沿用环境变量里的代理（trust_env）。"""
        return httpx2.AsyncClient(
            verify=os.path.abspath(Config.ca_bundle) if Config.ca_bundle else True,
            timeout=httpx2.Timeout(timeout, connect=10),
        )

    @staticmethod
    def _build_agent(
        *,
        http_client,
        output_type,
        instructions,
        tools=(),
        toolsets=(),
        deps_type=None,
        profile=None,
    ) -> Agent:
        provider = OpenAIProvider(
            base_url=Config.llm_api_base,
            api_key=Config.llm_api_key,
            http_client=http_client,
        )
        model = OpenAIChatModel(Config.llm_model, provider=provider, profile=profile)
        kwargs: dict = {}
        if deps_type is not None:
            kwargs["deps_type"] = deps_type
        return Agent(
            model,
            output_type=output_type,
            instructions=instructions,
            tools=tools,
            toolsets=toolsets,
            retries=2,
            **kwargs,
        )

    @staticmethod
    def _prepare_tools(req: ReviewRequest) -> tuple[RepoContext | None, list]:
        """按配置装配内置工具；开关关闭时不挂载。"""
        if not Config.agent_tools_enabled:
            return None, []
        deps = RepoContext(
            repo=req.repo,
            pr_number=req.pr_number,
            ref=req.head_sha,
            github_token=req.github_token or "",
        )
        return deps, build_tools()

    async def _run_agent(
        self,
        prompt: str,
        instructions: str,
        profile,
        deps: RepoContext | None = None,
        tools: list = (),
        toolsets: list = (),
    ) -> ReviewReport:
        # 客户端随本次运行创建 / 关闭：`review()` 内部用 asyncio.run，
        # 不能跨 event loop 复用连接池。
        client = self._http_client(timeout=Config.llm_timeout)
        try:
            agent = self._build_agent(
                http_client=client,
                output_type=ReviewReport,
                instructions=instructions,
                tools=tools,
                toolsets=toolsets,
                deps_type=type(deps) if deps is not None else None,
                profile=profile,
            )
            limits = UsageLimits(request_limit=Config.agent_max_turns or 2)
            if deps is not None:
                result = await agent.run(prompt, deps=deps, usage_limits=limits)
            else:
                result = await agent.run(prompt, usage_limits=limits)
            return result.output
        finally:
            await client.aclose()

    @staticmethod
    def _build_prompt(req: ReviewRequest) -> str:
        diff_text = build_diff_text(req.files, Config.max_diff_chars)
        parts = [
            f"仓库：{req.repo}",
            f"PR 编号：#{req.pr_number}",
            f"PR 标题：{req.pr_title}",
            "",
            f"<intent>\n{req.intent}\n</intent>",
            "",
        ]
        if req.extra:
            parts += [f"<extra>\n{req.extra}\n</extra>", ""]
        parts.append(f"<diff>\n{diff_text}\n</diff>")
        return "\n".join(parts)
