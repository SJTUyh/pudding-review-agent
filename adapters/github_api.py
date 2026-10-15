"""GitHub REST 操作：读 PR/文件、查用户、以检视人真名发表 review。"""
from typing import Any
import requests


class GitHubApi:
    def __init__(self, api_base: str):
        self.api_base = api_base.rstrip("/")

    # ---------- 用户 ----------
    def get_user(self, login: str) -> dict | None:
        r = requests.get(
            f"{self.api_base}/users/{login}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=30,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ---------- PR ----------
    def get_pull(self, headers: dict, repo: str, pr_number: int) -> dict:
        r = requests.get(
            f"{self.api_base}/repos/{repo}/pulls/{pr_number}", headers=headers, timeout=30
        )
        r.raise_for_status()
        return r.json()

    def get_pull_files(self, headers: dict, repo: str, pr_number: int) -> list[dict]:
        """分页拉取全部变更文件（含 patch）。"""
        files: list[dict] = []
        page = 1
        while True:
            r = requests.get(
                f"{self.api_base}/repos/{repo}/pulls/{pr_number}/files",
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=60,
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files

    def list_assigned_prs(self, headers: dict) -> list[dict]:
        """以某检视人身份列出「指派给我的全部 open issue」，再筛出其中的 PR。

        GET /issues?filter=assigned 返回该用户被指派的所有 issue，
        每条含 repository 字段；带 pull_request 字段的即为 PR。
        用检视人自己的 token 调用，故可覆盖其可访问的任意仓库。
        """
        result: list[dict] = []
        page = 1
        while True:
            r = requests.get(
                f"{self.api_base}/issues",
                headers=headers,
                params={"filter": "assigned", "state": "open", "per_page": 100, "page": page},
                timeout=60,
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            for item in batch:
                if "pull_request" not in item:
                    continue
                repo = (item.get("repository") or {}).get("full_name") or self._repo_from_url(
                    item.get("repository_url", "")
                )
                if not repo:
                    continue
                result.append(
                    {
                        "repo": repo,
                        "number": item["number"],
                        "title": item.get("title", ""),
                        "html_url": item.get("html_url", ""),
                        "assignees": [a["login"] for a in item.get("assignees", [])],
                    }
                )
            if len(batch) < 100:
                break
            page += 1
        return result

    @staticmethod
    def _repo_from_url(url: str) -> str:
        """'https://api.github.com/repos/owner/repo' -> 'owner/repo'。"""
        marker = "/repos/"
        idx = url.find(marker)
        return url[idx + len(marker):] if idx != -1 else ""

    # ---------- 真名发表（能力 3 出口）----------
    def post_review_as_user(
        self,
        user_token: str,
        repo: str,
        pr_number: int,
        findings: list[dict[str, Any]],
        summary: str,
    ) -> dict:
        """以检视人身份发 review；行内注释失败时降级为 issue comment。"""
        headers = {
            "Authorization": f"Bearer {user_token}",
            "Accept": "application/vnd.github+json",
        }
        comments = [
            {
                "path": f["path"],
                "line": f["line"],
                "side": "RIGHT",
                "body": _tag(f["body"]),
            }
            for f in findings
            if f.get("path") and f.get("line")
        ]
        payload: dict[str, Any] = {"body": _tag(summary), "event": "COMMENT"}
        if comments:
            payload["comments"] = comments

        r = requests.post(
            f"{self.api_base}/repos/{repo}/pulls/{pr_number}/reviews",
            headers=headers,
            json=payload,
            timeout=60,
        )
        if r.status_code == 422 and comments:
            # 行号不合法等导致 422：退化为纯 body 的 issue comment
            body = _tag(summary) + "\n\n" + "\n\n".join(
                f"**`{f['path']}`"
                + (f":{f['line']}" if f.get("line") else "")
                + f"**\n{_tag(f['body'])}"
                for f in findings
            )
            r2 = requests.post(
                f"{self.api_base}/repos/{repo}/issues/{pr_number}/comments",
                headers=headers,
                json={"body": body},
                timeout=60,
            )
            if not r2.ok:
                raise RuntimeError(_gh_error(r2))
            return r2.json()
        if not r.ok:
            raise RuntimeError(_gh_error(r))
        return r.json()


REVIEW_TAG = "[review]"


def _tag(text: str) -> str:
    """给检视意见正文加上统一标签前缀（已带则不重复加）。"""
    text = (text or "").strip()
    return text if text.startswith(REVIEW_TAG) else f"{REVIEW_TAG} {text}".rstrip()


def _gh_error(r: requests.Response) -> str:
    """把 GitHub 返回的 message/errors 拼进异常，方便回显到飞书。"""
    try:
        data = r.json()
    except ValueError:
        return f"GitHub HTTP {r.status_code}: {r.text[:300]}"
    msg = data.get("message", "")
    detail = ""
    errs = data.get("errors")
    if errs:
        detail = "；".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errs)
    tail = f"（{detail}）" if detail else ""
    return f"GitHub HTTP {r.status_code}: {msg}{tail}"
