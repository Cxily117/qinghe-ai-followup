# 清禾 · 医院随访智能体系统（qinghe-ai-followup）

面向医院出院患者随访场景的智能体系统：制度知识库问答（RAG）、随访任务管理、红旗症状升级闭环、质控度量。架构按全院级平台设计，落地分期走。

> 想快速看懂仓库？先读 `docs/repo-map.md`（每个文件是干什么的、该给谁看），再看 `src/MVP功能对照.md`（交付清单每一项对应哪段代码）。

## 仓库三分区

| 目录 | 内容 | 给谁看 |
|---|---|---|
| `docs/` | 调研与商务文档，按 1–9 编号：访谈记录 / 项目简报 / 典型事件 / 行业分析 / 流程图 / 需求定义 / 项目设计方案 / 报价方案 / 交付运行手册，另有 `10-MVP项目说明` 与 repo-map、demo-script 等辅助说明 | 院方评审、商务沟通 |
| `src/` | **MVP 项目本体**：FastAPI 工程（工程架构 / Agent 框架 / 知识库 / 5 处人工确认点 / admin-staff 双角色）+ 单测 + 冒烟脚本 + 前端 + 迁移 | 开发、运维 |
| `test/` | 测试证据：量化测试方案、五类场景过程记录、上线部署地址 | 验收、质检 |

## docs 阅读顺序

1. `docs/1-访谈记录/` → 六人访谈原始记录（事实基线）
2. `docs/3-典型事件/` + `docs/4-行业分析/` → 痛点 P1–P6 与行业判断
3. `docs/5-流程图/` → 改造前（含痛点标注）与改造后对比
4. `docs/6-需求定义/` → 需求定义文档
5. `docs/7-项目设计方案/` → 四层架构、12 张表、第一周最小验证
6. `docs/8-报价方案/` → 三档三方式 + 投入产出（双口径回本期）
7. `docs/9-交付运行手册/` → 部署、日常运行、故障处理、升级回滚
8. `docs/10-MVP项目说明/` → 代码导读：五个交付要素分别落在哪

## src：MVP 工程

FastAPI（异步）+ SQLAlchemy 2.0 + Alembic + JWT + RAG + Jinja2 管理后台。开发态 SQLite 零依赖可跑，生产切 PostgreSQL+pgvector。

| 交付要素 | 代码位置 |
|---|---|
| 工程架构 | `src/app/`（`main.py` 入口 / `api/` / `services/` / `agents/` / `models/` / `core/`） |
| Agent 框架 | `src/app/agents/`（runtime 执行循环、rag 检索问答、tools+gateway、guardrails 护栏） |
| 知识库 | `src/app/models/`（制度文档+分块）、`src/app/services/knowledge_service.py`、`src/app/api/v1/knowledge.py` |
| 人工确认/审核点 ×5 | 任务草稿确认、制度发布、归档、智能体草稿确认、提示词保存校验 |
| 权限 ×2 | `src/app/models/user.py` 的 `role`（admin/staff）+ `src/app/core/deps.py` 的 `require_admin` |
| 问答助手归档 | `src/app/models/chat.py`（sessions/messages 落库）、`src/app/models/audit.py`（脱敏留痕）、`src/scripts/archive_audit.py`（冷归档+哈希校验） |

```bash
cd src
pip install -r requirements.txt
alembic upgrade head            # 建表
python run_server.py            # 医护问答 http://127.0.0.1:8000/
                                # 管理后台 http://127.0.0.1:8000/admin/login

# 端到端冒烟（另开终端，需先启动服务）
python scripts/smoke_test.py --base-url http://127.0.0.1:8000
```

日常开发用 `src/Makefile` 统一入口：`make lint` / `make fmt` / `make test` / `make check`（CI 等价）/ `make migrate` / `make run` / `make smoke`。

生产（pgvector）：`docker compose up -d` → `.env` 设 `DATABASE_URL=postgresql+asyncpg://followup:followup@localhost:5433/followup` → `alembic upgrade head`。
默认 `mock` 供应商（无 Key 也能跑通全链路）；接真实模型只改环境变量：`LLM_PROVIDER=openai` + `EMBED_PROVIDER=openai` + `LLM_API_BASE/LLM_API_KEY`。

## 工程质量基线

| 项 | 现状 |
|---|---|
| 配置中心 | `src/pyproject.toml`（ruff lint/format + pytest + coverage 统一在此） |
| 静态检查 | `ruff check` 全绿（E/W/F/I/UP/B/BLE/ISC/C4/RUF） |
| 格式 | `ruff format --check` 全绿（107 个文件） |
| 提交前 | `src/.pre-commit-config.yaml`（ruff + 私钥检测 + 大文件拦截） |
| CI | `.github/workflows/ci.yml`：lint → format → `alembic upgrade head` → pytest --cov → 应用可导入 |
| 迁移 | Alembic 5 个版本化迁移，CI 每轮从零建库验证 |
| 单测 | 54 例全绿（护栏 / 脱敏 / 限流 / 规则 / 安全） |
| 密钥纪律 | `.env`、`*.db`、`.venv`、部署副本均不入库，仅提供 `.env.example` |

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| D1–D2 | 认证（JWT/PBKDF2）+ RBAC + 6 张表 + 双方言迁移 | ✅ |
| D3–D4 | 知识库入库（切片→向量化→发布）+ `/chat` 流式 RAG（混合检索 + 护栏 + 溯源） | ✅ |
| D5 | 任务草稿生成（function calling，AI 只到 draft，confirmed 必须人工点） | ✅ |
| 阶段 3 | 管理后台：知识库管理（含 PDF）/ 提示词在线编辑（保存即生效）/ 对话审计与高危标记 | ✅ |
| D6 | 质控报表（完成率 / 红旗升级率 / 无引用回答占比 / AI 草稿修改率） | ⬜ |

已实测：D1–D2 的 14 项接口回归；冒烟脚本在 mock 与真实模型（glm-4-flash + embedding-3）两种模式下全绿；
D5 新增 5 组断言（客户端硬塞 `status=confirmed` 仍落 draft、缺患者标识 422、越权确认 404、人工确认 confirmed、重复确认/取消 409）；
医护问答前端（单页 + 打字机 + 检索状态 + 引用卡片可展开原文）与管理后台 7 个页面均逐页无头浏览器截图核验，冒烟 21 步全绿。
详细取舍见 `src/README.md`、`src/MVP功能对照.md`。

## 阶段 1 原型（历史，保留可跑）

`prototype/` 是命令行 RAG 原型：读取随访制度 markdown → 500 字切片 → BM25 + 向量混合检索 → LLM 回答（带引用、拒答护栏）。

```bash
cd prototype
pip install faiss-cpu numpy rank_bm25 jieba httpx
python rag_cli.py 随访制度示例.md   # 离线联调（无需 API Key）
```

真实模型实测记录（glm-4-flash + embedding-3）：关键词问「骨科术后随访节点」引用正确；语义问「患者发烧到 39 度」命中「体温超过 38.5℃」片段；超范围问「化疗后用什么药」正确拒答。

## License

内部项目文档，未授权前请勿外传。
