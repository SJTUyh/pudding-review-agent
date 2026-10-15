"""飞书适配器：长连接收事件 + 发消息/交互卡片 + 卡片构造。"""
import json
import os
import threading
import time
from typing import Callable
import requests

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)


class FeishuClient:
    """基于 tenant_access_token 的发送客户端（发消息、发卡片）。"""

    def __init__(self, app_id: str, app_secret: str, api_base: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base = api_base.rstrip("/")
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._lock = threading.Lock()

    def tenant_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            r = requests.post(
                f"{self.api_base}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
            self._token = data["tenant_access_token"]
            self._token_exp = time.time() + int(data.get("expire", 7200))
            return self._token

    def _send(self, open_id: str, msg_type: str, content: dict | str) -> dict:
        body = {
            "receive_id": open_id,
            "msg_type": msg_type,
            "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
        }
        r = requests.post(
            f"{self.api_base}/im/v1/messages",
            params={"receive_id_type": "open_id"},
            headers={
                "Authorization": f"Bearer {self.tenant_token()}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def send_text(self, open_id: str, text: str) -> dict:
        return self._send(open_id, "text", {"text": text})

    def send_card(self, open_id: str, card: dict) -> dict:
        return self._send(open_id, "interactive", card)


# ---------------- 卡片构造 ----------------

def card_authorize(user_code: str, verification_uri: str, scope: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "需要授权 GitHub 真名身份"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "为了以**你本人**的身份在 PR 上发表检视意见，需要你授权一次。\n"
                        f"1. 打开：{verification_uri}\n"
                        f"2. 输入验证码：**{user_code}**\n"
                        f"3. 同意授权（scope: `{scope}`）\n\n"
                        "授权完成后回来，我会自动继续。"
                    ),
                },
            },
        ],
    }


def _severity_badge(sev: str) -> str:
    return {
        "critical": "🟥 critical",
        "major": "🟧 major",
        "minor": "🟨 minor",
        "info": "🟦 info",
    }.get(sev, f"⬜ {sev}")


def card_confirm_draft(
    draft_id: int, repo: str, pr_number: int, summary: str, findings: list[dict]
) -> dict:
    lines = [f"**{len(findings)} 条**检视意见待确认：\n"]
    for i, f in enumerate(findings[:20], 1):
        loc = f.get("path", "?")
        if f.get("line"):
            loc += f":{f['line']}"
        lines.append(
            f"{i}. [`{loc}`] {_severity_badge(f.get('severity', 'info'))}\n   {f.get('body', '')}"
        )
    if len(findings) > 20:
        lines.append(f"\n… 其余 {len(findings) - 20} 条省略。")
    body_md = "\n".join(lines)

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": f"检视草稿 {repo} #{pr_number}"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**总结**：{summary or '（无）'}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": body_md}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "确认后将以你的名义发布到 PR"}]},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认发布"},
                        "type": "primary",
                        "value": {"action": "publish", "draft_id": draft_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "丢弃"},
                        "type": "danger",
                        "value": {"action": "discard", "draft_id": draft_id},
                    },
                ],
            },
        ],
    }


def card_assignment(pr_number: int, title: str, html_url: str, login: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": "你被指派了 PR"}},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"[#{pr_number} {title}]({html_url})\n\n"
                        f"@你（`{login}`）已被指派为该 PR 的检视人。\n"
                        f"回复 `/review {html_url} <你的检视重点>` 即可让我按你的意图生成检视意见。"
                    ),
                },
            },
        ],
    }


def card_notice(title: str, content_md: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content_md}}],
    }


# ---------------- 长连接 ----------------

def _extract_text(content_json: str) -> str:
    try:
        data = json.loads(content_json or "{}")
    except (ValueError, TypeError):
        return ""
    text = data.get("text", "")
    # 去掉 @ 提到的占位符
    return " ".join(part for part in text.split() if not part.startswith("@_")).strip()


def _card_response(toast_content: str, toast_type: str = "info"):
    """构造 card.action.trigger 的即时响应（卡片回调需 3 秒内 ACK）。"""
    return P2CardActionTriggerResponse(
        {"toast": {"type": toast_type, "content": toast_content}}
    )


def _apply_ws_proxy() -> None:
    """让飞书长连接走代理。

    lark SDK 的 websockets.connect 固定传 proxy=None（强制直连），在「系统代理」
    网络下会 `timed out during opening handshake`。这里改掉 SDK 的连接参数：
    显式走代理，并附上自定义 CA（代理做了 TLS 中间人时才能校验通过）。
    """
    proxy = next(
        (
            os.environ[k]
            for k in (
                "FEISHU_WS_PROXY",
                "WSS_PROXY",
                "HTTPS_PROXY",
                "https_proxy",
                "ALL_PROXY",
                "all_proxy",
                "HTTP_PROXY",
                "http_proxy",
            )
            if os.environ.get(k)
        ),
        "",
    )
    if not proxy:
        print("[feishu] 未检测到代理，长连接将直连")
        return
    import inspect
    import ssl

    import websockets
    from lark_oapi.ws import client as ws_client

    if "proxy" not in inspect.signature(websockets.connect).parameters:
        print("[feishu] 当前 websockets 版本不支持 proxy 参数，长连接仍会直连")
        return

    kwargs: dict = {"proxy": proxy}
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if ca and os.path.exists(ca):
        kwargs["ssl"] = ssl.create_default_context(cafile=ca)
    ws_client._ws_connect_kwargs = lambda: kwargs
    print(f"[feishu] 长连接走代理：{proxy}" + ("（自定义 CA）" if "ssl" in kwargs else ""))


def build_ws_client(
    app_id: str,
    app_secret: str,
    on_message: Callable[[str, str, str], None],
    on_card: Callable[[str, dict, str], None],
):
    """构建飞书长连接客户端。

    on_message(open_id, text, event_id)
    on_card(open_id, value, event_id)
    """
    _apply_ws_proxy()

    def _do_message(data) -> None:
        try:
            event = data.event
            if event.message.chat_type != "p2p":
                return
            open_id = event.sender.sender_id.open_id
            text = _extract_text(event.message.content)
            event_id = data.header.event_id if data.header else ""
            on_message(open_id, text, event_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[feishu] on_message error: {exc}")

    def _do_card(data):
        try:
            event = data.event
            open_id = event.operator.open_id
            value = dict(event.action.value or {})
            event_id = data.header.event_id if data.header else event.token or ""
            on_card(open_id, value, event_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[feishu] on_card error: {exc}")
        return _card_response("处理中…")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_do_message)
        .register_p2_card_action_trigger(_do_card)
        .build()
    )
    return lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.INFO)
