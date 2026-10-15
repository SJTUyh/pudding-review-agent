"""意图检视引擎：拼装上下文 → 调用 LLM → 解析结构化 findings。"""
import json
import re
from pathlib import Path
from typing import Any
import requests

_SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / "intent_review.md"

_VALID_SEVERITY = {"critical", "major", "minor", "info"}


def load_skill_prompt() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


def build_diff_text(files: list[dict], max_chars: int) -> str:
    """把 PR 变更文件拼成带路径标注的 diff 文本，超限截断。"""
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
        parts.append("\n> 注意：diff 过大已被截断，仅依据以上内容检视。\n")
    return "\n".join(parts)


def call_llm(
    api_base: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = 120,
) -> str:
    """OpenAI 兼容的 Chat Completions 调用，返回助手文本。"""
    url = api_base.rstrip("/") + "/chat/completions"
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        },
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def _extract_json(text: str) -> dict:
    """从模型输出中提取 JSON：优先 ```json 代码块，其次首个平衡的 {...}。"""
    if not text:
        return {}
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = m.group(1) if m else None
    if raw is None:
        start = text.find("{")
        if start == -1:
            return {}
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    raw = text[start : i + 1]
                    break
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _normalize_findings(raw: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        body = str(item.get("body", "")).strip()
        if not path or not body:
            continue
        sev = str(item.get("severity", "info")).strip().lower()
        if sev not in _VALID_SEVERITY:
            sev = "info"
        try:
            line = int(item["line"]) if item.get("line") is not None else None
        except (ValueError, TypeError):
            line = None
        out.append({"path": path, "line": line, "severity": sev, "body": body})
    return out


class IntentReviewEngine:
    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        timeout: int,
        max_diff_chars: int,
    ):
        self.api_base = api_base
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_diff_chars = max_diff_chars
        self.system_prompt = load_skill_prompt()

    def review(
        self,
        intent: str,
        repo: str,
        pr_number: int,
        pr_title: str,
        files: list[dict],
        extra: str = "",
    ) -> dict:
        diff_text = build_diff_text(files, self.max_diff_chars)
        user_prompt = (
            f"仓库：{repo}\n"
            f"PR 编号：#{pr_number}\n"
            f"PR 标题：{pr_title}\n\n"
            f"<intent>\n{intent}\n</intent>\n\n"
            f"{('<extra>' + chr(10) + extra + chr(10) + '</extra>' + chr(10) + chr(10)) if extra else ''}"
            f"<diff>\n{diff_text}\n</diff>\n"
        )
        content = call_llm(
            self.api_base,
            self.api_key,
            self.model,
            self.system_prompt,
            user_prompt,
            self.timeout,
        )
        parsed = _extract_json(content)
        return {
            "summary": str(parsed.get("summary", "")).strip() or "（模型未返回总结）",
            "findings": _normalize_findings(parsed.get("findings")),
        }
