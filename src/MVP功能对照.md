# MVP 功能对照（交付清单 → 代码位置）

> 本文件是 src 代码区的导览：交付清单 src 项的每个要求，对应到下面的真实代码。
> 代码文件名保持英文（工程惯例），本表给出「清单用语 → 代码路径」的一一对照。

## 1. 问答助手归档

| 代码位置 | 作用 |
|---|---|
| `app/models/chat.py` | 会话（sessions）/ 消息（messages）全量落库，含 citations 引用元数据——问答记录归档的库表 |
| `app/models/audit.py` + `app/services/audit_service.py` | 审计留痕：操作与决策日志，入库前递归脱敏（住院号/姓名） |
| `scripts/archive_audit.py` | 到期数据归档脚本：gzip + sha256 + manifest，先校验再删 |
| `app/services/report_service.py` + `app/admin/` | 质控报表与后台记录页，归档数据可查可导出 |

## 2. MVP 项目五要素

### 2.1 工程架构（四层）

| 层 | 代码位置 |
|---|---|
| 入口层 | `app/main.py` + `app/api/v1/`（REST 接口）+ `app/admin/`（管理后台） |
| 服务层 | `app/services/`（业务服务，不 import 任何 LLM SDK） |
| 智能体编排层 | `app/agents/`（唯一调用大模型的层） |
| 数据模型层 | `app/models/`（12 张表）+ `alembic/versions/`（迁移 0001–0005） |

### 2.2 Agent 框架

| 代码位置 | 作用 |
|---|---|
| `app/agents/runtime.py` | 执行循环：逐步调用工具、逐轮决策留痕 |
| `app/agents/rag.py` | 检索增强问答：强制带 [1][2] 引用编号 |
| `app/agents/tools.py` + `app/agents/gateway/` | Function Calling 工具与大模型供应商适配（可替换） |
| `app/agents/guardrails.py` | 护栏：无据不作答、来源清理、改写不叠加 |

### 2.3 知识库

| 代码位置 | 作用 |
|---|---|
| `app/models/kb.py` | 制度文档 + 分块（向量 1024 维）两张表 |
| `app/services/knowledge_service.py` + `app/services/doc_parser.py` | 上传解析、分块向量化、检索 |
| `app/api/v1/knowledge.py` | 上传 / 发布 / 归档接口（draft → published → archived 生命周期） |

### 2.4 人工确认 / 审核点（≥3 处，实际 5 处）

| # | 代码位置 | 内容 |
|---|---|---|
| 1 | `app/api/v1/tasks.py` → `confirm` 接口 | 生成任务止步草稿，必须归属人确认（AI 无法调用） |
| 2 | `app/api/v1/knowledge.py` → `publish_document` | 制度发布审核（草稿不进检索） |
| 3 | `app/api/v1/knowledge.py` → `archive_document` | 制度归档/下架审核 |
| 4 | `app/api/v1/agent.py` → `runs/{uid}/confirm` | 智能体待落库草稿的人工确认 |
| 5 | `app/api/v1/prompts.py` 保存校验 + 审计人工标记 | 提示词变更 422 拦截、审计复核 |

### 2.5 权限（≥2 个角色）

| 代码位置 | 作用 |
|---|---|
| `app/models/user.py` | `role` 字段：`admin`（管理员）/ `staff`（普通员工）双角色 |
| `app/core/deps.py` | `require_admin` 依赖注入：管理接口强制 admin，越权 403 |

## 测试证据

- 单元测试：`tests/`（54 例，不依赖模型/网络）
- 端到端冒烟：`scripts/smoke_test.py`（28 段 103 断言，权限场景 49 条）
- 测试文档与场景过程：仓库顶层 `test/` 目录
