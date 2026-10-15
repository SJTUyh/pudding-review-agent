"""GitHub OAuth App：Device Flow 为检视人换取真名 token（gho_）。

OAuth App 基于「用户授权」，token 覆盖该用户可访问的全部资源，
因此可在**别人的开源仓库**以检视人本人身份发评论。
"""
import requests


class GitHubOAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        scope: str,
        api_base: str,
        device_code_url: str,
        token_url: str,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.api_base = api_base.rstrip("/")
        self.device_code_url = device_code_url
        self.token_url = token_url

    def start_device_flow(self) -> dict:
        """第一步：拿设备码和验证地址。

        返回含 device_code / user_code / verification_uri / interval / expires_in。
        """
        r = requests.post(
            self.device_code_url,
            data={"client_id": self.client_id, "scope": self.scope},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def poll_device_token(self, device_code: str) -> dict:
        """第二步：轮询换 token（OAuth App 的 Device Flow 不需要 client_secret）。

        - 成功：{access_token, token_type, scope, refresh_token?, expires_in?}
        - 未完成：{error: authorization_pending|slow_down|expired_token|access_denied}
        """
        r = requests.post(
            self.token_url,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def refresh(self, refresh_token: str) -> dict:
        """App 开启 token 过期时，用 refresh token 续期。"""
        r = requests.post(
            self.token_url,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def whoami(self, access_token: str) -> dict:
        """用 user token 查当前用户，确认真名身份（login）。"""
        r = requests.get(
            f"{self.api_base}/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
