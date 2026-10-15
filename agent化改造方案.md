# Agent 化改造方案

> 目标：把「所有需要模型处理的信息」统一收敛到一个**可插拔的 Agent 层**，并把首个后端接到
> **Pydantic AI**（带工具自主检索 + MCP 扩展通道）。
>
> 适用仓库：`review-agent`（PR 代码检视助手，能力 3 真名检视 + 能力 4 飞书交互）

---

## 1. 现状盘点

当前「需要模型处理」的位置只有 2 处，都是**裸 `requests.post` 打 `/chat/completions`**：

| 位置 | 文件 | 作用 |
|---|---|---|
| `call_llm()` | [engine/intent_review.py](file:///d:/for_developing/review_agent/engine/intent_review.py#L40-L68) | 生成检视 findings |
| 启动自检探针 | [app/main.py](file:///d:/for_developing/review_agent/app/main.py#L450-L460) | 连通性探测 |

配套的「手写胶水」：

- `_extract_json()` —— 用正则从模型输出里**抠 JSON**（[intent_review.py#L71-L96](file:///d:/for_developing/review_agent/engine/intent_review.py#L71-L96)）
- `_normalize_findings()` —— 手工容错（字段缺失丢弃、severity 不在白名单降级为 info）
- `build_diff_text()` —— 把 diff 拼成一段文本，**超 `MAX_DIFF_CHARS` 直接截断**（[intent_review.py#L17-L37](file:///d:/for_developing/review_agent/engine/intent_review.py#L17-L37)）
- `IntentReviewEngine` —— 固定 system prompt（读 `skills/intent_review.md`）+ 单次 user prompt

**三个真实痛点**：

1. 输出靠正则兜底，**没有 schema 约束**，解析失败就静默返回空结果。
2. 模型**只能看到被截断的 diff**，无法按需读仓库里的相关文件（"这个函数在别处怎么被调用的" 它答不了）。
3. 模型调用散落在业务代码里，加一个新能力（PR 描述、评论回复、通知摘要）就得再抄一遍 `call_llm`。

---

## 2. 目标与原则

1. **单一入口**：除 `agents/` 之外的任何模块**都不允许**直接调用模型（不 import 任何 LLM SDK、不发 `/chat/completions`）。全部经由 `agents.registry.get_backend()`。
2. **可插拔**：后端按名字注册，通过环境变量选择；支持第三方通过 Python **entry points** 注册新后端，**不改本仓库代码**即可接入。
3. **强结构化输出**：findings / summary 用 Pydantic 模型约束，替换正则抠 JSON。
4. **能力可降级**：网关若不支持 tool calling，自动降级为单次推理（prompt 与输出结构不变）。
5. **依赖可控**：Agent 框架依赖放进**可选 extra**，不引入框架时服务仍能跑（走轻量后端）。

---

## 3. 选型结论

| 项 | 选择 | 依据 |
|---|---|---|
| 首个 Agent 后端 | **Pydantic AI** | MIT；`pydantic-ai-slim` 依赖轻，与本项目极简栈气质一致；`output_type` 是**真正的 Pydantic schema 约束**（直接消灭正则抠 JSON）；`OpenAIProvider(base_url=...)` 一行接自建网关；原生 MCP（stdio / SSE / streamable-HTTP）；async-first 契合任务队列 |
| 检索方式 | **工具自主检索** | agent 可主动读仓库文件 / 搜索代码 / 拉 PR 上下文，摆脱 `MAX_DIFF_CHARS` 截断 |
| 工具扩展 | **内置工具 + MCP 双通道** | 日后接 `mcp-server-git`、代码图谱类 MCP server 只改配置 |
| 备选后端（不实现，仅留位） | OpenAI Agents SDK / PR-Agent | Agents SDK：轻、原生 MCP、handoffs；PR-Agent：业界最成熟的 PR 检视成品，Apache-2.0，但要求 Python ≥ 3.12、依赖重（litellm 等），且设计上"自己直接往 PR 发评论"，与"人工确认后再以真名发布"冲突，需额外改造层 |

> 参考来源：Pydantic AI 官方文档（Models / Output / MCP Client / Installation-TLS）、OpenAI Agents SDK 文档（0.22.0，Python ≥ 3.10）、Qodo「PR-Agent 交还社区并改回 Apache-2.0」公告、PyPI `pr-agent`（Python ≥ 3.12）。

---

## 4. 目标架构

```
                    ┌──────────────────────────────┐
  app/main.py ──────┤ agents.registry.get_backend()│
  (自检/草稿/未来)   └──────────────┬───────────────┘
                                   │ ReviewBackend (Protocol)
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
   backends/pydantic_ai.py    backends/echo.py    (第三方 entry points)
              │
              ├── tools/repo_tools.py   内置工具：读文件 / 搜索 / PR 上下文
              └── tools/mcp_client.py    MCP servers（stdio / HTTP，配置驱动）
```

### 4.1 目录结构（新增）

```
agents/
  __init__.py
  base.py                    # ReviewRequest / Finding / ReviewResult / ReviewBackend(Protocol)
  registry.py                # 后端注册与发现（内置 + entry points），AGENT_BACKEND 选择
  schemas.py                 # Pydantic 模型：Finding / ReviewReport（强结构化输出）
  prompts.py                 # 载入 skills/*.md 作为 instructions
  backends/
    __init__.py
    pydantic_ai_backend.py   # 首个后端
    echo.py                  # 离线/单测用，无外部依赖
  tools/
    __init__.py
    repo_tools.py            # 内置 GitHub 工具（用检视人 gho_ token，不 clone）
    mcp_client.py            # 从配置装配 MCP server（MCPToolset）
```

改造：

- `engine/intent_review.py` —— **保留 `IntentReviewEngine` 对外签名**，内部改为「构造 `ReviewRequest` → 交给 registry」；删除 `call_llm()` / `_extract_json()`（`_normalize_findings` 的职责移交 Pydantic 校验）。
- `app/main.py` —— 自检的 `_llm()` 改为调 `backend.probe()`。
- `config.py` —— 新增 Agent 段配置。
- `requirements.txt` —— 新增 `pydantic-ai-slim`（可选 extra）。

### 4.2 核心接口（`agents/base.py`）

```python
@dataclass
class Finding:
    path: str
    line: int | None
    severity: Literal["critical", "major", "minor", "info"]
    body: str

@dataclass
class ReviewRequest:
    intent: str                  # 检视人的意图/重点
    repo: str                    # owner/name
    pr_number: int
    pr_title: str
    files: list[dict]            # PR 变更文件（含 patch）
    extra: str = ""
    head_sha: str | None = None  # 工具检索用的 ref
    github_token: str | None = None  # 检视人 gho_ token，供工具访问 GitHub

@dataclass
class ReviewResult:
    summary: str
    findings: list[Finding]

class ReviewBackend(Protocol):
    name: str
    def available(self) -> tuple[bool, str]: ...      # (是否可用, 原因)
    def probe(self) -> str: ...                       # 连通性自检，返回描述
    def review(self, req: ReviewRequest) -> ReviewResult: ...
```

**约定**：`review()` 是**同步**方法（内部 `asyncio.run`），以适配现有 `queue.Queue` + 线程 worker 的模型；
未来若要异步化，另加 `areview()`。

### 4.3 注册与发现（`agents/registry.py`）

- 内置后端：`pydantic_ai`（默认）、`echo`（离线/测试）。
- 第三方后端：`importlib.metadata.entry_points(group="review_agent.agents")`，第三方包只需在 `pyproject.toml` 里声明该 entry point。
- 选择与降级：`AGENT_BACKEND`（默认 `pydantic_ai`）→ 不可用时按 `AGENT_FALLBACK` 链降级（默认 `echo` 兜底并告警，**不静默失败**）。

### 4.4 工具层（`agents/tools/`）

内置工具（全部基于检视人 `gho_` token 走 GitHub API，**不需要 clone 仓库**）：

| 工具 | 作用 | 用的接口 |
|---|---|---|
| `list_pr_files()` | 变更文件清单（path/status/增删行数） | `GET /repos/{r}/pulls/{n}/files` |
| `get_file(path, ref)` | 取仓库任意文件**全文**（绕开 diff 截断） | `GET /repos/{r}/contents/{path}?ref=` |
| `get_file_patch(path)` | 取该文件**完整** patch（无截断） | 同上 files 结果 |
| `search_code(q)` | 在仓库内搜索代码 | `GET /search/code?q=repo:{r}+{q}` |
| `get_pr_commits()` | 提交信息（理解改动意图） | `GET /repos/{r}/pulls/{n}/commits` |

MCP 通道：`tools/mcp_client.py` 按 `AGENT_MCP_SERVERS`（JSON）装配 `MCPToolset`，
支持 stdio / streamable-HTTP 两种传输；默认空（不启用）。示例：

```json
[{"name":"git","transport":"stdio","command":"uvx","args":["mcp-server-git","--repository","/path/to/repo"]}]
```

### 4.5 强结构化输出（`agents/schemas.py`）

```python
class FindingModel(BaseModel):
    path: str
    line: int | None = None
    severity: Literal["critical", "major", "minor", "info"] = "info"
    body: str

class ReviewReport(BaseModel):
    summary: str
    findings: list[FindingModel] = []
```

通过 `Agent(output_type=ReviewReport)` 交给框架做**原生 schema 约束 + 校验重试**，
替代 `_extract_json()` 的正则兜底。

### 4.6 配置项（`config.py` 新增）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_BACKEND` | `pydantic_ai` | 选用的后端 |
| `AGENT_FALLBACK` | `echo` | 降级链（逗号分隔） |
| `AGENT_TOOLS_ENABLED` | `true` | 是否启用工具自主检索 |
| `AGENT_MAX_TURNS` | `8` | 工具调用轮数上限 |
| `AGENT_MCP_SERVERS` | 空 | MCP server 配置（JSON 数组） |
| `LLM_*` / `MAX_DIFF_CHARS` | 不变 | 沿用现有配置 |

---

## 5. 落地步骤

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0 骨架** | `agents/base.py` + `registry.py` + `echo` 后端；`app/main.py` 自检改走 `backend.probe()` | 不引入任何新依赖，服务照常启动，自检仍全绿 |
| **P1 接 Pydantic AI** | `backends/pydantic_ai_backend.py`：`OpenAIProvider(base_url=LLM_BASE_URL)` + `output_type=ReviewReport`，**先只做单次推理**；`engine/intent_review.py` 切到 registry；删掉 `call_llm`/`_extract_json` | 同一 PR 的 findings 与现状等价或更好；解析失败率归零 |
| **P2 工具检索** | `tools/repo_tools.py` 五个内置工具 + `AGENT_TOOLS_ENABLED` + `AGENT_MAX_TURNS`；不支持 tool calling 时自动降级为单次推理 | 提出"读某文件确认调用方"类问题能被 agent 自行回答；长 diff 不再因截断丢信息 |
| **P3 MCP 通道** | `tools/mcp_client.py` + `AGENT_MCP_SERVERS`；默认关闭，附一个 `mcp-server-git` 示例配置 | 只改 `.env` 即可挂载一个新 MCP server |
| **P4 收尾** | `requirements.txt` 加 `pydantic-ai-slim`（extra）；`.env.example` 补配置；`能力3-4实现方案.md` 标注被本方案取代的部分；自检输出后端名与工具数 | 按 `.env.example` 可一键复现 |

---

## 6. 风险与验证

| 风险 | 说明 | 对策 |
|---|---|---|
| **网关不支持 tool calling** | 第三方 OpenAI 兼容网关常在 `tools`/`tool_calls` 上有兼容缺口；本项目模型是 `deepseek-v4.1-flash`，**未验证** | 上 P2 前先写 **30 行 smoke test**：经代理 + 自签 CA 指向网关，验证「基础对话 + 一次 tool call + 一次 `output_type` 结构化输出」三件事同时通；不通就走降级路径 |
| **TLS / 自签 CA 不被框架认** | Pydantic AI 自身 HTTP 走**操作系统信任库**，不读 `REQUESTS_CA_BUNDLE`；而 `config.py` 目前只注入了 `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` | 显式注入自定义 httpx 客户端：`OpenAIProvider(openai_client=AsyncOpenAI(base_url=..., http_client=httpx.AsyncClient(verify=Config.ca_bundle, proxy=...)))`；MCP 侧用其 `ssl_ctx` 参数。**这条是"换完框架就 SSL 报错"的高发点** |
| **代理** | 现有走系统代理（`http://127.0.0.1:789x`）+ TLS 中间人 | httpx 认 `HTTPS_PROXY`/`NO_PROXY`；自签 CA 必须显式 `verify=` |
| **异步与线程队列** | 后端是 async-first，现有 worker 是线程 + `queue.Queue` | 在 worker 线程内 `asyncio.run(agent.run(...))`，**不跨线程复用 event loop** |
| **时延与成本** | 多轮工具调用会显著变慢、变贵 | `AGENT_MAX_TURNS` 限流 + 保留 `MAX_DIFF_CHARS` 作为首轮上下文上限 |
| **Python 版本** | Pydantic AI 对 Python 版本有下限要求 | 落地前先确认 WSL 里的 Python 版本，并做一次 `pip install` 试装 |
| **AGPL 印记** | 若日后引入 PR-Agent | 其已从 AGPL 改回 Apache-2.0（社区托管），但仍需在引入时复核当版 LICENSE |

---

## 7. 与既有文档的关系

- 本方案**不改变**能力 3 / 能力 4 的产品形态：「意图驱动 → 生成草稿 → 检视人确认 → 以真名发布 → 统一 `[review]` 标签」全流程保持不变。
- 仅替换「模型调用与结果解析」这一层，并给模型补上**主动检索代码**的能力。
- `能力3-4实现方案.md` §3（引擎）中关于 `call_llm` / 正则解析的描述，由本方案 §4 取代。
