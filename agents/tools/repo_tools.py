"""内置检索工具：让 agent 用检视人自己的 `gho_` token 自主读 PR 上下文。

设计要点：

1. **不需要 clone 仓库** —— 全部走 GitHub REST；
2. **同步实现** —— pydantic-ai 会把同步工具丢进线程池（`anyio.to_thread.run_sync`），
   与 `adapters/` 现有 `requests` 风格保持一致，不会阻塞 event loop；
3. **上下文从 deps 取** —— `RunContext[RepoContext]` 携带 repo / PR 号 / ref / token，
   工具的**入参只暴露模型真正该决定的东西**（文件路径、搜索词）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import requests
from pydantic_ai import RunContext

from adapters.github_api import GitHubApi
from config import Config

# 单次工具返回的字符上限，避免把上下文撑爆
_MAX_TOOL_CHARS = 20000


@dataclass
class RepoContext:
    """一次检视的工具运行上下文。"""

    repo: str
    pr_number: int
    ref: str | None = None
    github_token: str = ""
    api_base: str = field(default_factory=lambda: Config.github_api_base)
    # 懒加载缓存：files / patch 会被多个工具复用
    _files: list[dict] | None = field(default=None, repr=False)

    @property
    def headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json"}
        if self.github_token:
            h["Authorization"] = f"Bearer {self.github_token}"
        return h

    @property
    def _api(self) -> GitHubApi:
        return GitHubApi(self.api_base)

    def files(self) -> list[dict]:
        if self._files is None:
            self._files = self._api.get_pull_files(self.headers, self.repo, self.pr_number)
        return self._files


def _truncate(text: str, limit: int = _MAX_TOOL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...（已截断，原文 {len(text)} 字符）"


def _gh_error(r: requests.Response) -> str:
    return f"GitHub HTTP {r.status_code}: {r.text[:300]}"


def list_pr_files(ctx: RunContext[RepoContext]) -> str:
    """列出本次 PR 变更的全部文件（路径、状态、增删行数）。"""
    rc = ctx.deps
    files = rc.files()
    rows = [
        {
            "path": f.get("filename"),
            "status": f.get("status"),
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
        }
        for f in files
    ]
    return _truncate(json.dumps(rows, ensure_ascii=False, indent=2))


def get_file(ctx: RunContext[RepoContext], path: str) -> str:
    """读取仓库中某个文件的完整内容。

    Args:
        path: 仓库相对路径，例如 `src/app/main.py`。
    """
    rc = ctx.deps
    params = {"ref": rc.ref} if rc.ref else {}
    headers = dict(rc.headers)
    headers["Accept"] = "application/vnd.github.raw"
    r = requests.get(
        f"{rc.api_base}/repos/{rc.repo}/contents/{path}",
        headers=headers,
        params=params,
        timeout=60,
    )
    if r.status_code == 404:
        return f"文件不存在：{path}（ref={rc.ref or 'default'}）"
    if not r.ok:
        return _gh_error(r)
    return _truncate(r.text)


def get_file_patch(ctx: RunContext[RepoContext], path: str) -> str:
    """读取某个变更文件的完整 diff（patch）。

    当主 prompt 里的 diff 因超长被截断时，用它取回完整内容。

    Args:
        path: 仓库相对路径，需与 `list_pr_files` 返回的路径一致。
    """
    rc = ctx.deps
    for f in rc.files():
        if f.get("filename") == path:
            patch = f.get("patch")
            if not patch:
                return f"{path} 无 patch（可能是二进制文件或改动过大被 GitHub 省略）。"
            return _truncate(patch)
    return f"{path} 不在本次 PR 的变更文件列表中。"


def search_code(ctx: RunContext[RepoContext], query: str) -> str:
    """在本仓库内搜索代码，用于确认「谁调用了这个函数」等跨文件问题。

    Args:
        query: 搜索关键词，例如 `def parse_config` 或 `IntentReviewEngine`。
    """
    rc = ctx.deps
    r = requests.get(
        f"{rc.api_base}/search/code",
        headers=rc.headers,
        params={"q": f"repo:{rc.repo} {query}", "per_page": 20},
        timeout=60,
    )
    if r.status_code in (401, 403):
        return (
            f"代码搜索不可用（HTTP {r.status_code}）：GitHub 的 code search 需要鉴权且限流较严，"
            "请改为先用 list_pr_files 找出相关文件，再用 get_file / get_file_patch 直接读取。"
        )
    if not r.ok:
        return _gh_error(r)
    items = (r.json() or {}).get("items", [])
    if not items:
        return f"未搜索到与 `{query}` 匹配的代码。"
    rows = [{"path": it.get("path"), "matches": (it.get("text_matches") or [])} for it in items]
    return _truncate(json.dumps(rows, ensure_ascii=False, indent=2))


def get_pr_commits(ctx: RunContext[RepoContext]) -> str:
    """列出本次 PR 的全部提交（sha、作者、提交信息）。"""
    rc = ctx.deps
    r = requests.get(
        f"{rc.api_base}/repos/{rc.repo}/pulls/{rc.pr_number}/commits",
        headers=rc.headers,
        params={"per_page": 100},
        timeout=60,
    )
    if not r.ok:
        return _gh_error(r)
    rows = [
        {
            "sha": (c.get("sha") or "")[:10],
            "author": (c.get("commit", {}).get("author") or {}).get("name"),
            "message": (c.get("commit", {}).get("message") or "").splitlines()[0],
        }
        for c in (r.json() or [])
    ]
    return _truncate(json.dumps(rows, ensure_ascii=False, indent=2))


def build_tools() -> list:
    """返回内置工具列表，交给 `Agent(tools=...)`。"""
    return [list_pr_files, get_file, get_file_patch, search_code, get_pr_commits]
