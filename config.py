"""集中配置：读取 .env，暴露一个全局 Config。

能力 3 / 4 只用 GitHub **OAuth App**（真名代理，可作用于任意开源仓库），
不需要 GitHub App / 私钥。
"""
import os
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """极简 .env 解析：KEY=VALUE，忽略空行与 # 注释，不覆盖已存在的环境变量。"""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_list(key: str, default: str = "") -> list[str]:
    return [x.strip() for x in _env(key, default).split(",") if x.strip()]


class Config:
    # --- GitHub OAuth App（真名代理的核心）---
    github_client_id: str = _env("GITHUB_CLIENT_ID")
    github_client_secret: str = _env("GITHUB_CLIENT_SECRET")
    # public_repo（仅公开库，推荐）或 repo（含私有库）
    github_oauth_scope: str = _env("GITHUB_SCOPE", "public_repo")
    # /review 用 #编号 简写时的默认仓库
    github_repo: str = _env("GITHUB_REPO", "AISBench/benchmark")
    github_api_base: str = _env("GITHUB_API_BASE", "https://api.github.com")
    github_device_code_url: str = _env(
        "GITHUB_DEVICE_CODE_URL", "https://github.com/login/device/code"
    )
    github_token_url: str = _env(
        "GITHUB_TOKEN_URL", "https://github.com/login/oauth/access_token"
    )

    # --- 飞书自建应用（机器人 + 长连接）---
    feishu_app_id: str = _env("FEISHU_APP_ID")
    feishu_app_secret: str = _env("FEISHU_APP_SECRET")
    feishu_api_base: str = _env("FEISHU_API_BASE", "https://open.feishu.cn/open-apis")

    # --- LLM（OpenAI 兼容端点，可切换）---
    llm_api_base: str = _env("LLM_BASE_URL", "https://api.openai.com/v1")
    llm_api_key: str = _env("LLM_API_KEY")
    llm_model: str = _env("LLM_MODEL", "gpt-4o-mini")
    llm_timeout: int = _env_int("LLM_TIMEOUT", 120)

    # --- Agent 层（所有模型调用的唯一入口）---
    # 选用的后端；内置：pydantic_ai / echo
    agent_backend: str = _env("AGENT_BACKEND", "pydantic_ai")
    # 后端不可用时的降级链（逗号分隔，按序下探）
    agent_fallback: list[str] = _env_list("AGENT_FALLBACK", "echo")
    # 是否允许 agent 调用工具自主检索（仓库文件 / 搜索 / PR 上下文 / MCP）
    agent_tools_enabled: bool = _env_bool("AGENT_TOOLS_ENABLED", True)
    # 工具调用轮数上限，防止多轮检索跑飞
    agent_max_turns: int = _env_int("AGENT_MAX_TURNS", 8)
    # MCP server 配置（JSON 数组），留空则不挂载
    agent_mcp_servers: str = _env("AGENT_MCP_SERVERS")

    # --- 本服务 ---
    fernet_key: str = _env("FERNET_KEY")
    db_path: str = _env("DB_PATH", "./data/review_agent.db")
    poll_interval: int = _env_int("POLL_INTERVAL", 60)
    max_diff_chars: int = _env_int("MAX_DIFF_CHARS", 120000)
    admin_github_login: str = _env("ADMIN_GITHUB_LOGIN")
    # 自定义 CA bundle（例如代理的中间人自签名根证书）；留空则用系统默认
    ca_bundle: str = _env("CA_BUNDLE")

    @classmethod
    def validate(cls) -> list[str]:
        """返回缺失的关键配置项列表，供启动时自检。"""
        missing = []
        if not cls.github_client_id:
            missing.append("GITHUB_CLIENT_ID")
        if not cls.github_client_secret:
            missing.append("GITHUB_CLIENT_SECRET")
        if not cls.feishu_app_id:
            missing.append("FEISHU_APP_ID")
        if not cls.feishu_app_secret:
            missing.append("FEISHU_APP_SECRET")
        if not cls.fernet_key:
            missing.append("FERNET_KEY")
        return missing


# 让 requests / ssl 信任自定义 CA（如代理的中间人根证书），需在其他库发起请求前设置。
if Config.ca_bundle:
    _ca = os.path.abspath(Config.ca_bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
    os.environ.setdefault("SSL_CERT_FILE", _ca)
