"""Agent 后端的注册与发现。

- 内置后端：`echo`、`pydantic_ai`
- 第三方后端：在自有包里声明 entry point（group = `review_agent.agents`），
  无需修改本仓库代码即可接入
- 选择：`AGENT_BACKEND` → 不可用时按 `AGENT_FALLBACK` 链降级（非静默，会打印告警）
"""
from __future__ import annotations

import importlib
import importlib.metadata
from typing import Any

from agents.base import ReviewBackend
from config import Config

ENTRY_POINT_GROUP = "review_agent.agents"

# 内置后端：名字 -> "模块路径:类名"
_BUILTIN_BACKENDS: dict[str, str] = {
    "echo": "agents.backends.echo:EchoBackend",
    "pydantic_ai": "agents.backends.pydantic_ai_backend:PydanticAiBackend",
}

_instances: dict[str, ReviewBackend] = {}


def _load_class(spec: str) -> Any:
    module_path, _, attr = spec.partition(":")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def discover() -> dict[str, str]:
    """返回全部可用后端：名字 -> "模块路径:类名"（内置 + entry points）。"""
    found = dict(_BUILTIN_BACKENDS)
    try:
        entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001 - 发现失败不应影响内置后端
        entry_points = []
    for ep in entry_points:
        if ep.value and ep.name not in found:
            found[ep.name] = ep.value
    return found


def _instantiate(name: str) -> ReviewBackend:
    if name in _instances:
        return _instances[name]
    specs = discover()
    if name not in specs:
        raise KeyError(f"未注册的后端 `{name}`（可选：{', '.join(sorted(specs))}）")
    cls = _load_class(specs[name])
    backend = cls()
    _instances[name] = backend
    return backend


def get_backend(name: str | None = None) -> ReviewBackend:
    """按 `AGENT_BACKEND`（或显式 name）取后端，不可用则沿降级链下探。"""
    requested = (name or Config.agent_backend or "echo").strip()
    chain = [requested] + [n for n in Config.agent_fallback if n and n != requested]

    problems: list[str] = []
    for candidate in chain:
        try:
            backend = _instantiate(candidate)
            ok, reason = backend.available()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{candidate} 加载失败：{exc}")
            continue
        if ok:
            if candidate != requested:
                print(
                    f"[agents] 后端 `{requested}` 不可用，已降级到 `{candidate}`。"
                    f"（{'；'.join(problems)}）"
                )
            return backend
        problems.append(f"{candidate} 不可用：{reason}")

    raise RuntimeError("没有可用的 Agent 后端 —— " + "；".join(problems))
