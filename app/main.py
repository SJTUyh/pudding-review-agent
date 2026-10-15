"""服务编排：飞书长连接 + 命令路由 + 任务队列 + GitHub 轮询 + 状态机。"""
import json
import queue
import re
import sys
import threading
import time
from typing import Any

import requests

from config import Config
from store.crypto import TokenCipher
from store.db import Database
from adapters.github_oauth import GitHubOAuth
from adapters.github_api import GitHubApi
from adapters import feishu_bot
from engine.intent_review import IntentReviewEngine, call_llm

_PR_URL_RE = re.compile(r"https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
_NUM_RE = re.compile(r"^#?(\d+)$")

HELP_TEXT = (
    "可用命令：\n"
    "1) /authorize            —— 授权 GitHub，绑定你的账号并取得真名发布权限\n"
    "2) /bind <github_login>  —— 仅绑定飞书与 GitHub 账号（用于接收通知）\n"
    "3) /review <PR链接或编号> <你的检视重点>  —— 按你的意图生成检视草稿\n"
    "4) /help                 —— 显示本帮助"
)


class ReviewService:
    def __init__(self):
        self.db = Database(Config.db_path)
        self.cipher = TokenCipher(Config.fernet_key)
        self.oauth = GitHubOAuth(
            Config.github_client_id,
            Config.github_client_secret,
            Config.github_oauth_scope,
            Config.github_api_base,
            Config.github_device_code_url,
            Config.github_token_url,
        )
        self.api = GitHubApi(Config.github_api_base)
        self.feishu = feishu_bot.FeishuClient(
            Config.feishu_app_id, Config.feishu_app_secret, Config.feishu_api_base
        )
        self.engine = IntentReviewEngine(
            Config.llm_api_base,
            Config.llm_api_key,
            Config.llm_model,
            Config.llm_timeout,
            Config.max_diff_chars,
        )

        self.tasks: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._pending_auth: dict[str, dict[str, Any]] = {}
        self._auth_lock = threading.Lock()
        self._stop = threading.Event()

    # ---------------- 启动 ----------------
    def start_background(self) -> None:
        for i in range(2):
            threading.Thread(target=self._worker, name=f"worker-{i}", daemon=True).start()
        threading.Thread(target=self._poller, name="poller", daemon=True).start()
        threading.Thread(target=self._auth_watcher, name="auth-watcher", daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()

    # ---------------- 飞书入口 ----------------
    def on_message(self, open_id: str, text: str, event_id: str) -> None:
        if not self.db.mark_event(event_id):
            return
        text = (text or "").strip()
        if not text:
            return
        low = text.lower()
        if low.startswith("/authorize"):
            self._cmd_authorize(open_id)
        elif low.startswith("/bind"):
            self._cmd_bind(open_id, text[len("/bind") :].strip())
        elif low.startswith("/review"):
            self._cmd_review(open_id, text[len("/review") :].strip())
        elif low.startswith("/help") or low.startswith("/start"):
            self.feishu.send_text(open_id, HELP_TEXT)
        else:
            self.feishu.send_text(
                open_id, "未识别的命令。\n\n" + HELP_TEXT
            )

    def on_card_action(self, open_id: str, value: dict, event_id: str) -> None:
        # 3 秒铁律：只入队，立即返回由 feishu_bot 完成
        if not self.db.mark_event(event_id):
            return
        self.tasks.put(("card", {"open_id": open_id, "value": value}))

    # ---------------- 命令处理 ----------------
    def _cmd_bind(self, open_id: str, login: str) -> None:
        if not login:
            self.feishu.send_text(open_id, "用法：/bind <github_login>")
            return
        if not self.api.get_user(login):
            self.feishu.send_text(open_id, f"GitHub 用户 `{login}` 不存在，请检查拼写。")
            return
        self.db.upsert_binding(login, open_id)
        self.feishu.send_text(
            open_id,
            f"已绑定 GitHub 账号 `{login}`。\n"
            "如需以真名发表检视意见，请再发送 /authorize 完成授权。",
        )

    def _cmd_authorize(self, open_id: str) -> None:
        data = self.oauth.start_device_flow()
        device_code = data["device_code"]
        with self._auth_lock:
            self._pending_auth[device_code] = {
                "open_id": open_id,
                "interval": int(data.get("interval", 5)),
                "expires_at": time.time() + int(data.get("expires_in", 900)),
                "last_poll": 0.0,
            }
        self.feishu.send_card(
            open_id,
            feishu_bot.card_authorize(
                data["user_code"], data["verification_uri"], Config.github_oauth_scope
            ),
        )

    def _cmd_review(self, open_id: str, args: str) -> None:
        binding = self.db.get_binding_by_open_id(open_id)
        if not binding:
            self.feishu.send_text(
                open_id, "你还没有绑定/授权 GitHub 账号，请先发送 /authorize。"
            )
            return
        repo, pr_number, focus = self._parse_review_args(args)
        if pr_number is None:
            self.feishu.send_text(
                open_id,
                "用法：/review <PR链接或编号> <检视重点>\n"
                "例如：/review https://github.com/AISBench/benchmark/pull/12 重点看并发安全",
            )
            return
        requester = binding["github_login"]
        intent_id = self.db.create_intent(repo, pr_number, requester, focus or "（未指定重点，请按通用最佳实践检视）")
        self.feishu.send_text(
            open_id, f"已收到你对 {repo} #{pr_number} 的检视意图，正在生成草稿，请稍候…"
        )
        self.tasks.put(
            ("draft", {"intent_id": intent_id, "open_id": open_id, "requester": requester})
        )

    def _parse_review_args(self, args: str) -> tuple[str, int | None, str]:
        parts = args.split(maxsplit=1)
        if not parts:
            return Config.github_repo, None, ""
        target = parts[0]
        focus = parts[1].strip() if len(parts) > 1 else ""
        m = _PR_URL_RE.search(target)
        if m:
            return m.group(1), int(m.group(2)), focus
        m2 = _NUM_RE.match(target)
        if m2:
            return Config.github_repo, int(m2.group(1)), focus
        return Config.github_repo, None, focus

    # ---------------- 授权轮询 ----------------
    def _auth_watcher(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_pending_auth()
            except Exception as exc:  # noqa: BLE001
                print(f"[auth] error: {exc}")
            self._stop.wait(3)

    def _poll_pending_auth(self) -> None:
        now = time.time()
        with self._auth_lock:
            codes = list(self._pending_auth.keys())
        for device_code in codes:
            with self._auth_lock:
                info = self._pending_auth.get(device_code)
            if not info:
                continue
            if now > info["expires_at"]:
                with self._auth_lock:
                    self._pending_auth.pop(device_code, None)
                self.feishu.send_text(info["open_id"], "授权已超时，请重新发送 /authorize。")
                continue
            if now - info["last_poll"] < info["interval"]:
                continue
            info["last_poll"] = now
            data = self.oauth.poll_device_token(device_code)
            err = data.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                info["interval"] = info["interval"] + 5
                continue
            if err:
                with self._auth_lock:
                    self._pending_auth.pop(device_code, None)
                self.feishu.send_text(info["open_id"], f"授权失败：{err}")
                continue
            token = data.get("access_token")
            if not token:
                continue
            with self._auth_lock:
                self._pending_auth.pop(device_code, None)
            self._on_authorized(info["open_id"], data)

    def _on_authorized(self, open_id: str, token_data: dict) -> None:
        try:
            user = self.oauth.whoami(token_data["access_token"])
        except Exception as exc:  # noqa: BLE001
            self.feishu.send_text(open_id, f"授权成功但获取用户信息失败：{exc}")
            return
        login = user["login"]
        self.db.upsert_binding(login, open_id)
        self._store_user_token(login, token_data)
        self.feishu.send_text(
            open_id,
            f"授权成功！已绑定 GitHub 账号 `{login}`。\n"
            "现在可以发送 /review <PR链接> <检视重点> 生成检视草稿。",
        )

    def _store_user_token(self, login: str, token_data: dict) -> None:
        access = token_data["access_token"]
        refresh = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in")
        expires_at = str(time.time() + int(expires_in)) if expires_in else None
        self.db.upsert_user_token(
            login,
            self.cipher.encrypt(access),
            self.cipher.encrypt(refresh) if refresh else None,
            expires_at,
            token_data.get("scope"),
        )

    def _user_token(self, login: str) -> str | None:
        """返回有效 access token；过期则用 refresh token 续期。"""
        row = self.db.get_user_token(login)
        if not row:
            return None
        exp = row["expires_at"]
        if exp and float(exp) < time.time() + 60:
            refresh = self.cipher.decrypt(row["refresh_enc"])
            if not refresh:
                return None
            data = self.oauth.refresh(refresh)
            if "access_token" not in data:
                return None
            self._store_user_token(login, data)
            return data["access_token"]
        return self.cipher.decrypt(row["access_enc"])

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

    # ---------------- 任务队列 ----------------
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                kind, payload = self.tasks.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if kind == "draft":
                    self._generate_draft(payload["intent_id"], payload["open_id"], payload["requester"])
                elif kind == "card":
                    self._process_card(payload["open_id"], payload["value"])
            except Exception as exc:  # noqa: BLE001
                print(f"[worker] task {kind} failed: {exc}")
            finally:
                self.tasks.task_done()

    def _generate_draft(self, intent_id: int, open_id: str, requester: str) -> None:
        intent = self.db.get_intent(intent_id)
        if not intent:
            return
        token = self._user_token(requester)
        if not token:
            self.db.set_intent_status(intent_id, "closed")
            self.feishu.send_text(
                open_id, "你的 GitHub 授权已失效，请重新发送 /authorize 后再试。"
            )
            return
        headers = self._headers(token)
        pr = self.api.get_pull(headers, intent["repo"], intent["pr_number"])
        files = self.api.get_pull_files(headers, intent["repo"], intent["pr_number"])
        result = self.engine.review(
            intent=intent["body"],
            repo=intent["repo"],
            pr_number=intent["pr_number"],
            pr_title=pr.get("title", ""),
            files=files,
        )
        draft_id = self.db.create_draft(
            intent_id=intent_id,
            repo=intent["repo"],
            pr_number=intent["pr_number"],
            reviewer=requester,
            findings=result,
            status="pending_confirm",
        )
        self.db.set_intent_status(intent_id, "consumed")
        if not result["findings"]:
            self.feishu.send_card(
                open_id,
                feishu_bot.card_notice(
                    f"检视完成 {intent['repo']} #{intent['pr_number']}",
                    f"针对你的意图未发现需要提出的问题。\n\n**总结**：{result['summary']}",
                ),
            )
            self.db.set_draft_status(draft_id, "rejected")
            return
        self.feishu.send_card(
            open_id,
            feishu_bot.card_confirm_draft(
                draft_id,
                intent["repo"],
                intent["pr_number"],
                result["summary"],
                result["findings"],
            ),
        )

    def _process_card(self, open_id: str, value: dict) -> None:
        action = value.get("action")
        draft_id = value.get("draft_id")
        if draft_id is None:
            return
        try:
            draft_id = int(draft_id)
        except (ValueError, TypeError):
            return
        draft = self.db.get_draft(draft_id)
        if not draft:
            return
        if draft["status"] != "pending_confirm":
            self.feishu.send_text(open_id, "该草稿已处理，无需重复操作。")
            return
        binding = self.db.get_binding_by_open_id(open_id)
        if not binding or binding["github_login"] != draft["reviewer"]:
            self.feishu.send_text(open_id, "只有该草稿对应的检视人本人才能操作。")
            return
        if action == "discard":
            self.db.set_draft_status(draft_id, "rejected")
            self.feishu.send_text(open_id, f"已丢弃草稿 #{draft_id}，未发布到 PR。")
            return
        if action == "publish":
            self._publish_draft(open_id, draft)

    def _publish_draft(self, open_id: str, draft) -> None:
        reviewer = draft["reviewer"]
        token = self._user_token(reviewer)
        if not token:
            self.feishu.send_text(
                open_id, "你的 GitHub 授权已失效，请重新发送 /authorize 后再试。"
            )
            return
        payload = json.loads(draft["findings"])
        try:
            resp = self.api.post_review_as_user(
                token,
                draft["repo"],
                draft["pr_number"],
                payload.get("findings", []),
                payload.get("summary", ""),
            )
        except Exception as exc:  # noqa: BLE001
            self.feishu.send_text(open_id, f"发布失败：{exc}")
            return
        self.db.set_draft_status(draft_id=draft["id"], status="published", comment_id=resp.get("id"))
        self.feishu.send_text(
            open_id,
            f"已以你的名义在 {draft['repo']} #{draft['pr_number']} 发布检视意见。",
        )

    # ---------------- GitHub 轮询（能力 4）----------------
    def _poller(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:  # noqa: BLE001
                print(f"[poller] error: {exc}")
            self._stop.wait(Config.poll_interval)

    def _poll_once(self) -> None:
        """每个已授权检视人各自以自己的 token 轮询『指派给我的 PR』。"""
        live_keys: set[tuple[str, int, str]] = set()
        live: list[tuple[dict, str]] = []
        for login in self.db.authorized_logins():
            token = self._user_token(login)
            if not token:
                continue
            try:
                prs = self.api.list_assigned_prs(self._headers(token))
            except Exception as exc:  # noqa: BLE001
                print(f"[poller] {login} 拉取失败: {exc}")
                continue
            for pr in prs:
                live.append((pr, login))
                live_keys.add((pr["repo"], pr["number"], login))

        known = self.db.current_assignments()
        for pr, login in live:
            key = (pr["repo"], pr["number"], login)
            if key not in known:
                self.db.add_assignment(*key)
                self._notify_assignee(login, pr)

        for key in set(known) - live_keys:
            self.db.remove_assignment(*key)

    def _notify_assignee(self, login: str, pr: dict) -> None:
        binding = self.db.get_binding(login)
        if binding:
            self.feishu.send_card(
                binding["feishu_open_id"],
                feishu_bot.card_assignment(pr["number"], pr.get("title", ""), pr["html_url"], login),
            )
            return
        if Config.admin_github_login:
            admin = self.db.get_binding(Config.admin_github_login)
            if admin:
                self.feishu.send_text(
                    admin["feishu_open_id"],
                    f"PR #{pr['number']} 指派给了 `{login}`，但该账号尚未绑定飞书，无法通知。\n"
                    f"请提醒 TA 私聊机器人发送 /bind {login} 完成绑定。",
                )


def _startup_selfcheck(service: "ReviewService") -> None:
    """启动自检：探测各外部依赖连通性，仅告警、不阻断启动。"""
    print("启动自检：")

    def _check(name: str, fn) -> None:
        try:
            detail = fn()
            print(f"  [OK] {name}" + (f"（{detail}）" if detail else ""))
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {name}：{exc}")

    def _llm() -> str:
        eng = service.engine
        call_llm(
            eng.api_base,
            eng.api_key,
            eng.model,
            "你是连通性探针，只回复 OK。",
            "回复 OK",
            timeout=15,
        )
        return f"model={eng.model}"

    def _feishu() -> str:
        return f"tenant_access_token len={len(service.feishu.tenant_token())}"

    def _github_api() -> str:
        r = requests.get(
            Config.github_api_base.rstrip("/") + "/users/octocat",
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        return f"api.github.com HTTP {r.status_code}"

    def _github_oauth_host() -> str:
        # github.com（Device Flow 主机）连通性：任何 HTTP 响应即视为可达
        r = requests.get("https://github.com/login/device", timeout=15)
        return f"github.com HTTP {r.status_code}"

    _check("LLM 模型", _llm)
    _check("飞书 tenant_access_token", _feishu)
    _check("GitHub API", _github_api)
    _check("GitHub OAuth(Device Flow 主机)", _github_oauth_host)


def main() -> None:
    missing = Config.validate()
    if missing:
        print("缺少必要配置：" + ", ".join(missing))
        print("请参考 .env.example 配置 .env 后重试。")
        sys.exit(1)

    service = ReviewService()
    _startup_selfcheck(service)
    service.start_background()
    print("review-agent 已启动：飞书长连接 + GitHub 轮询")

    ws = feishu_bot.build_ws_client(
        Config.feishu_app_id,
        Config.feishu_app_secret,
        on_message=service.on_message,
        on_card=service.on_card_action,
    )
    try:
        ws.start()  # 阻塞运行，SDK 自带断线重连
    except KeyboardInterrupt:
        pass
    finally:
        service.shutdown()


if __name__ == "__main__":
    main()
