# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 与用户协作的规则

1. **称呼**：每次回复前必须先称呼用户为 "Bing"。
2. **决策确认**：遇到不确定的代码设计问题时，必须先询问 Bing，得到确认后再行动，不得自行决定。
3. **代码兼容性**：除非 Bing 主动要求，否则不要编写兼容性代码（例如为旧接口、旧数据或旧行为保留的回退分支、版本判断、弃用别名等）。

## 仓库信息

- GitHub 远程仓库：https://github.com/blicme/Orbit-Agent.git

## 项目概览

EvoAgent 是一个 PR/diff 代码评审服务：接收统一 diff（HTTP API、GitHub webhook 或异步队列），运行确定性规则扫描和可选的 LLM 多 Agent 评审，再通过证据/置信度门禁决定发布哪些问题，并持久化任务、执行轨迹、报告、记忆和评测数据。

Python 3.11，HTTP 服务、鉴权和 Agent Runtime 都是标准库实现。运行依赖见 `requirements.txt`（psycopg、redis、PyJWT、OpenTelemetry、PyYAML）。没有构建步骤。

## 常用命令

```bash
python -m pip install -r requirements.txt
python -m evoagent                        # 启动 HTTP 服务，默认 127.0.0.1:8080，/ 提供 web/ 管理台
python -m unittest discover -s tests -v   # 全量测试
python -m unittest discover -s tests -p "test_reviewer.py" -v   # 只跑一个测试模块
```

Docker（PostgreSQL + Redis + 服务）：

```bash
docker compose up --build
```

评测脚本：

```bash
python scripts/run_pr_diff_benchmark.py pr_diff_100.jsonl --reviewer local
python scripts/run_pr_diff_benchmark.py pr_diff_100.jsonl --reviewer agentic --limit 5
```

`--reviewer local` 不需要模型；`openai` 和 `agentic` 通过 `Settings.from_env()`（`.env` 或进程环境变量）解析凭据。`pr_diff_100.jsonl` 是脚本引用的标注数据集。

测试说明：
- 测试使用标准库 `unittest`，没有 pytest 配置；`tests/` 没有 `__init__.py`，`test_service.py`、`test_advanced.py` 依赖 `from agentic_fake import ...`，因此用 `discover -s tests` 运行最稳妥。
- Windows 上全量测试会有约 40 个 teardown `PermissionError`（SQLite 文件仍被占用），以及一个既有的 `test_advanced.test_safe_fixer_changes_only_supported_rules` 引号断言失败。判断回归时对比改动前后的失败集合，不要把这些当作新问题。

## 配置

`evoagent/config.py` 在 import 时调用 `load_dotenv()` 读取项目根目录 `.env`，使用 `os.environ.setdefault`，所以**进程环境变量优先于 `.env`**。所有配置集中在 `Settings.from_env()`，`ReviewService` 初始化时由 `Settings.validate_evolution()` 校验。

- `EVOAGENT_LLM_PROVIDER=local`（默认）只跑确定性规则，服务可不配置模型启动；`agentic` 评审必须有 `deepseek`、`openrouter-*` 或 `custom` 模型。
- `EVOAGENT_DATABASE_URL` 非空时从 SQLite 切到 PostgreSQL；`EVOAGENT_REDIS_URL` 非空时从进程内队列切到 Redis Streams。
- 影响耗时的关键项：`EVOAGENT_REVIEW_EFFORT`（`auto|full|fast`）、`EVOAGENT_FAST_PATH_MAX_ADDED_LINES`、`EVOAGENT_FAST_PATH_MAX_FILES`、`EVOAGENT_MAX_REVISION_ROUNDS`、`EVOAGENT_AGENT_TOKEN_BUDGET`、`EVOAGENT_AGENT_TIME_BUDGET_SECONDS`。

## 架构

请求路径：`api.py`（标准库 `ThreadingHTTPServer`）→ `ReviewService`（`service.py`）→ `ReviewHarness`（`harness.py`）→ `AgentRuntime`（`runtime.py`）的 `planning → executing → reviewing` 三个节点 → `ModeRouterReviewer`（`agentic_core.py`）→ `FindingGate`（`gates.py`）→ `TaskStore`（`store.py` / `postgres_store.py`）。

核心约束：

- **Runtime 检查点**：每个节点结果按节点名持久化；`ReviewHarness.run` 会恢复已完成的检查点，任务续跑不会重复已完成阶段。`executing` 失败时节点会重试（默认 1 次），并复用已保存的 Lead session。
- **Agentic 会话**：agentic 模式唯一入口是 `ModeRouterReviewer.review_with_context()`。流程为确定性扫描 → Lead 委派 → Security/Correctness Worker（`ThreadPoolExecutor` 并行）→ Lead 评估/返工 → Critic 盲审 → Lead 汇总。会话以 `agentic-lead-session` 检查点保存，可幂等恢复。
- **评审深度**：`EVOAGENT_REVIEW_EFFORT=auto`（默认）时，小 diff（新增行 ≤ 阈值且文件数 ≤ 阈值）走 `_single_pass()`，只调用一次模型并与规则结果合并；`full` 强制完整层级，`fast` 强制单次评审。显式请求 Agent Skill 时不会走快速通道。
- **模型动作协议**：`BoundedRole` 期望 `{"action":"tool",...}` 或 `{"action":"final",...}` JSON。`action_kind()` 兼容缺失 `action`、同义 `type` 字段以及直接返回 phase 载荷的情况。预算或步数耗尽时，改为一次禁用工具的“收尾调用”，而不是抛错丢弃已有证据。
- **模型传输**：`JsonChatClient`（以及 `OpenAICompatibleReviewer`）要求响应是 JSON 对象。`decode_json_object()` 接受代码围栏、前后说明文字、多个对象和截断 JSON；仍无法解析时只做一次定向修复调用，之后抛 `ValueError`（不重试）。
- **门禁是最终裁决**：`FindingGate` 会丢弃位置不在新增行、critical/high 缺少强证据或调用链、置信度低于 0.55、以及高危但缺少 fix/test 的问题。新增任何问题来源都必须提供 `evidence_refs` 或 `call_chain`。
- **Skill 是提示词能力**：`skills/<name>/SKILL.md` 是 YAML frontmatter + Markdown，由 `SkillRegistry` 加载；Lead 为 Worker 选择 Skill，Skill 的 `allowed-tools` 会与 Worker 的 `ROLE_PERMISSIONS` 求交集。同一个 registry 也承载 Python 扫描器（`SecurityRuleReviewer`、`ReliabilityRuleReviewer`）。
- **存储接口**：`postgres_store.py` 的 `create_store()` 返回 `TaskStore`（SQLite）或 `PostgresTaskStore`。改表结构必须同时改两边。
- **队列**：`TaskQueue` 默认用 `ThreadPoolExecutor`；配置 `EVOAGENT_REDIS_URL` 后使用 Redis Streams，带 ACK、租约、退避和死信队列。
- **可观测性**：`telemetry.ExecutionLedger` 记录每次模型/工具调用和 Agent trace，会写入报告 `execution` 字段并随检查点持久化；`observability.py` 提供 OTel span 和告警，`metrics.py` 在 `/metrics` 暴露 Prometheus 指标。

## 修改入口

- 确定性规则：`reviewer.py` 的 `LocalRuleReviewer.RULES` 及 Domain reviewer 的 `rule_ids`。
- Agent 工具：`repository_tools.py` 的 `registry()` 规格 + `agentic_core.py` 的 `ROLE_PERMISSIONS`。
- HTTP 路由：`api.py` 顶部路由正则 + `do_GET` / `do_POST` 分支。
- 新配置项：`config.py` dataclass 字段 + `from_env()` + `.env.example` + README。
- 提示词：`agentic_core.py` 的 `LEAD_PROMPT`、`SECURITY_PROMPT`、`RELIABILITY_PROMPT`、`CRITIC_PROMPT`、`SINGLE_PASS_PROMPT`；已激活的 `llm-review` 版本可从 store 覆盖。
- Skill 包：`skills/<name>/SKILL.md`，frontmatter 需要 `name`、`description`，可选 `allowed-tools`。
- 持久化：`store.py` 与 `postgres_store.py` 保持同步。
- 评测：`evaluation_harness.py`（用例格式与指标）、`evaluation_v2.py`（产品分支/消融）、`scripts/` 下的入口脚本。

## 容易踩的坑

- `load_dotenv()` 在 import `evoagent.config` 时就执行，会向 `os.environ` 写入 `.env` 中尚未设置的键；测试里直接构造 `Settings(...)` 不会读取 `.env`。
- 只设置 `EVOAGENT_LLM_PROVIDER=local` 无法执行 agentic 评审，会明确报错要求配置模型。
- `EVOAGENT_AGENT_TOKEN_BUDGET` 统计的是角色所有调用的输入 + 输出 token，多步工具调用消耗很快；优先用评审深度、返工轮数和收尾调用配置控制耗时，不要盲目调大 `max_steps`。
- `web/` 在 `/` 和 `/assets/*` 提供；前端登录态保存在浏览器 `localStorage`。
- `output/`、`evoagent.db` 是本地产物，已被 git 忽略。