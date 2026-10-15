"""可选 MCP 通道：把外部 MCP server 的工具挂到 agent 上。

默认 **不启用**，通过 `AGENT_MCP_SERVERS`（JSON 数组）配置。每条支持两种形态：

- stdio（本地子进程，需自行安装对应的 MCP server，例如 `npx -y @modelcontextprotocol/server-git`）::

    [{"name": "git", "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-git", "--repository", "/path/to/repo"]}]

- 远端 HTTP/SSE::

    [{"name": "remote", "url": "http://127.0.0.1:8000/mcp", "headers": {"Authorization": "Bearer x"}}]

模块导入是惰性的：未安装 `pydantic-ai-slim[mcp]` 时仅打印告警，不影响内置工具。
"""
from __future__ import annotations

import json

from config import Config


def build_mcp_toolsets() -> list:
    """按配置装配 MCP toolsets；未配置 / 未安装 extra 时返回空列表。"""
    raw = (Config.agent_mcp_servers or "").strip()
    if not raw:
        return []

    try:
        from pydantic_ai.mcp import (
            FastMCPClient,
            MCPToolset,
            SSETransport,
            StdioTransport,
            StreamableHttpTransport,
            infer_transport_type_from_url,
        )
    except ImportError:
        print(
            "[agents] 已配置 AGENT_MCP_SERVERS，但未安装 MCP 依赖；"
            "请执行 pip install 'pydantic-ai-slim[mcp]'。本次跳过 MCP。"
        )
        return []

    try:
        servers = json.loads(raw)
    except ValueError as exc:
        print(f"[agents] AGENT_MCP_SERVERS 不是合法 JSON，已跳过 MCP：{exc}")
        return []
    if not isinstance(servers, list):
        print("[agents] AGENT_MCP_SERVERS 应为 JSON 数组，已跳过 MCP。")
        return []

    toolsets: list = []
    for item in servers:
        if not isinstance(item, dict):
            print(f"[agents] 跳过非法 MCP 配置项：{item!r}")
            continue
        name = str(item.get("name") or "mcp")
        url = item.get("url")
        command = item.get("command")
        try:
            if url:
                if infer_transport_type_from_url(url) == "sse":
                    transport = SSETransport(
                        url, headers=item.get("headers"), verify=item.get("verify")
                    )
                else:
                    transport = StreamableHttpTransport(
                        url, headers=item.get("headers"), verify=item.get("verify")
                    )
            elif command:
                transport = StdioTransport(
                    command,
                    list(item.get("args") or []),
                    env=item.get("env"),
                    cwd=item.get("cwd"),
                )
            else:
                print(f"[agents] MCP 配置项缺少 url/command，已跳过：{name}")
                continue
            toolsets.append(MCPToolset(FastMCPClient(transport, name=name)))
            print(f"[agents] 已挂载 MCP server：{name}")
        except Exception as exc:  # noqa: BLE001 - 单个 server 配错不应拖垮整次检视
            print(f"[agents] MCP server {name} 装配失败，已跳过：{type(exc).__name__}: {exc}")
    return toolsets
