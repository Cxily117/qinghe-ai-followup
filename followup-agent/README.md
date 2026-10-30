# followup-agent · 阶段 2 D1–D2 骨架

FastAPI（异步）+ SQLAlchemy 2.0 + Alembic + JWT。范围：**认证 + 6 张表 + 迁移**，业务能力（RAG 对话 / 知识库 / 任务生成 / 报表）留到 D3–D6。

## 快速启动（开发态，SQLite 零依赖）

```bash
cd followup-agent
pip install -r requirements.txt

# 1）建表
alembic upgrade head

# 2）启动
uvicorn app.main:app --reload
# 打开 http://127.0.0.1:8000/docs
```

## 生产态（PostgreSQL 16 + pgvector）

```bash
docker compose up -d          # 起 pgvector/pgvector:pg16（端口 5433）
# .env 中设置：
#   DATABASE_URL=postgresql+asyncpg://followup:followup@localhost:5433/followup
alembic upgrade head           # 自动 CREATE EXTENSION vector + HNSW 索引
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口清单（D1–D2）

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/health` | 健康检查 | 公开 |
| POST | `/api/v1/auth/register` | 注册（演示允许注册 admin） | 公开 |
| POST | `/api/v1/auth/login` | 登录换取 JWT | 公开 |
| GET | `/api/v1/auth/me` | 当前用户 | 登录 |
| GET | `/api/v1/tasks/drafts` | 我的任务草稿 | 登录 |
| POST | `/api/v1/tasks` | 生成任务草稿（D5） | 登录 · 501 |
| GET | `/api/v1/kb/documents` | 制度文档列表 | 管理员 |
| POST | `/api/v1/kb/documents` | 上传文档（D3–D4） | 管理员 · 501 |
| GET | `/api/v1/reports/summary` | 质控汇总（D6） | 管理员 |
| POST | `/api/v1/chat` | 对话（D3–D4） | 登录 · 501 |

## 数据模型（6 张表）

`users` / `sessions` / `messages` / `kb_documents` / `kb_chunks` / `followup_task_drafts`
详见 `随访智能体技术架构方案.md` 第 4 节。

关键设计：
- `messages.meta` JSONB/JSON 存引用 chunk id、模型、token 用量、耗时（溯源与质控）
- `kb_documents.status` 两态（draft/published），RAG 只检索 published
- `followup_task_drafts.status` 止步 draft，confirmed 必须人工（硬关卡）
- `kb_chunks.embedding`：PostgreSQL=pgvector `vector(1024)` + HNSW；SQLite=JSON 降级

## 目录

```
followup-agent/
├── app/
│   ├── main.py            # 入口
│   ├── core/              # config / security(JWT+PBKDF2) / database / deps(RBAC)
│   ├── api/v1/            # auth · chat · tasks · knowledge · reports
│   ├── models/            # 6 张表 ORM
│   └── schemas/           # Pydantic DTO
├── alembic/               # env.py（URL 从 Settings 注入）+ versions/0001
├── docker-compose.yml     # pgvector/pgvector:pg16（5433）
├── requirements.txt
└── .env.example
```

## 下一步（D3–D4）

- 知识库上传：解析 → 500 字切片（复用 `prototype/rag_cli.py` 的 chunk 逻辑）→ embeddings → kb_chunks
- `/chat` 流式：混合检索（BM25 + 向量）→ LLM 网关（供应商可配）→ messages.meta 落引用
