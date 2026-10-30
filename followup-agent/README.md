# followup-agent · 阶段 2（D1–D4）

FastAPI（异步）+ SQLAlchemy 2.0 + Alembic + JWT + RAG。

- **D1–D2**：认证（JWT/PBKDF2）+ RBAC + 6 张表 + 双方言迁移 ✅
- **D3–D4**：知识库入库（切片→向量化→发布）+ `/chat` 流式 RAG（混合检索 + 护栏 + 溯源落库）✅
- **D5–D6**：任务草稿生成（function calling）、质控报表 ⬜

## 快速启动（开发态，SQLite 零依赖）

```bash
cd followup-agent
pip install -r requirements.txt

alembic upgrade head          # 建表
uvicorn app.main:app --reload # http://127.0.0.1:8000/docs
```

默认 `LLM_PROVIDER=mock` / `EMBED_PROVIDER=mock`：**没有 API Key 也能跑通整条链路**
（mock 的向量无语义，回答是回显式的，只用于验证管道）。

## 接真实模型（零代码改动，只改环境变量）

```bash
export LLM_PROVIDER=openai
export LLM_API_BASE=https://open.bigmodel.cn/api/paas/v4   # 任何 OpenAI 兼容地址
export LLM_API_KEY=***
export LLM_CHAT_MODEL=glm-4-flash
export EMBED_PROVIDER=openai                                # 不设这个 = 向量仍走 mock
export EMBED_MODEL=embedding-3
export EMBED_DIM=1024                                        # 必须等于 pgvector 的 vector(N)
uvicorn app.main:app --port 8000
```

> 注意：**换了 embedding 模型或改过 `EMBED_DIM` 之后，必须重建知识库切片**，
> 否则新旧向量不同空间，检索会给出无意义结果（代码会检测维度不一致并显式报错）。

## 生产态（PostgreSQL 16 + pgvector）

```bash
docker compose up -d          # pgvector/pgvector:pg16 → 端口 5433（避让本机 PG13）
# .env: DATABASE_URL=postgresql+asyncpg://followup:followup@localhost:5433/followup
alembic upgrade head          # 自动 CREATE EXTENSION vector + vector(1024) + HNSW 索引
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口清单

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/health` | 健康检查（含当前 provider） | 公开 |
| POST | `/api/v1/auth/register` | 注册（演示允许注册 admin） | 公开 |
| POST | `/api/v1/auth/login` | 登录换取 JWT | 公开 |
| GET | `/api/v1/auth/me` | 当前用户 | 登录 |
| POST | `/api/v1/kb/documents` | 上传制度文档（md/txt，自动切片+向量化，落 draft） | 管理员 |
| POST | `/api/v1/kb/documents/{id}/publish` | 发布（进入检索，旧版自动归档） | 管理员 |
| GET | `/api/v1/kb/documents` | 文档列表（含切片数） | 管理员 |
| GET | `/api/v1/kb/documents/{id}/chunks` | 查看切片（核对切片质量） | 管理员 |
| GET | `/api/v1/kb/stats` | 知识库统计（已发布切片数） | 管理员 |
| POST | `/api/v1/chat` | **SSE 流式**问答 | 登录 |
| POST | `/api/v1/chat/ask` | 非流式问答（脚本/兜底） | 登录 |
| POST | `/api/v1/chat/sessions` | 新建会话 | 登录 |
| GET | `/api/v1/chat/sessions` | 我的会话列表 | 登录 |
| GET | `/api/v1/chat/sessions/{id}/messages` | 会话消息（含溯源 meta） | 登录（仅本人） |
| GET | `/api/v1/tasks/drafts` | 我的任务草稿 | 登录 |
| POST | `/api/v1/tasks` | 生成任务草稿（D5） | 登录 · 501 |
| GET | `/api/v1/reports/summary` | 质控汇总（D6） | 管理员 |

### SSE 事件序列

```
event: session     data: {"session_id":1}
event: citations   data: {"citations":[{"n":1,"chunk_id":2,"doc_title":"...","preview":"..."}]}
event: delta       data: {"text":"骨科术后随访节点为"}          # 多帧
event: replace     data: {"text":"..."}                        # 仅在护栏改写输出时出现
event: done        data: {"message_id":9,"final_text":"...","citations":[...],"flags":{},"latency_ms":3182}
event: end
```

前端渲染建议：先渲染 `citations`（让用户看到"依据"），再按 `delta` 打字机式追加；
收到 `replace` 就整体覆盖（拒答去引用、越界引用剔除、追加免责时会发生）。

## D3–D4 的关键设计

| 设计 | 为什么 |
|---|---|
| **两态文档**：上传=draft，检索只认 published | 未经签核的制度，AI 不能拿去回答护士的问题；这是合规线，必须写在代码里而不是流程里 |
| **版本策略**：同名再上传 version+1，发布新版自动 archived 旧版 | 满足留痕，同时保证"同一时点只有一版生效" |
| **混合检索** BM25(jieba) + 向量，min-max 归一化后加权 | 关键词问（"第7天"）与语义问（"发烧39度" vs 原文"38.5℃"）都要准 |
| **检索在库内完成**，不引入独立向量服务 | 少一套必须与业务库同步的中间件；试点规模（几千切片）单次查询 < 100ms |
| **护栏做在代码里**，不只写在提示词里 | 提示词是"请求"，代码是"保证"：越界引用 `[9]`、拒答带引用都要确定性拦掉 |
| **流式接口自建 DB 会话** | FastAPI 0.106+ 的 yield 依赖在响应开始前就退出，复用注入的 session 写库会炸 |
| **助手消息必须落 meta** | `citations / model / latency_ms / prompt_version / flags` 是"AI 当时依据什么说的"唯一可举证处 |

### 输出护栏（`app/agents/guardrails.py`）

| 规则 | 行为 | meta 标记 |
|---|---|---|
| 拒答却标了引用 | 强制清除引用标记 | `refusal: true` |
| 引用编号越界（如 `[9]` 但只有 3 条资料） | 删除该编号 | `dropped_citations: [9]` |
| 有实质内容但零引用 | 不改内容，打标供质控抽查 | `uncited_answer`, `suspected_general_knowledge` |
| 出现用药/剂量表述 | 追加固定免责声明 | `medical_advice_suspected` |
| 问题含红旗症状关键词 | 打标，便于统计"AI 是否提示升级" | `red_flag_question` |
| 知识库为空/未发布 | 不调模型，直接拒答 | `empty_kb` |

已知取舍：模型"答一半 + 拒一半"时引用也会被清空。这是**故意偏保守**——
"拒答却带着资料编号"看起来像编造出处，比"少显示两条来源"严重得多。

## 冒烟测试（可挂 CI）

```bash
uvicorn app.main:app --port 8000 &
python scripts/smoke_test.py --base-url http://127.0.0.1:8000
```

覆盖：健康检查 → 注册登录 → 上传(draft) → **发布前提问不命中该文档** → 发布 →
**发布后命中并引用** → SSE 事件完整性 → `messages.meta` 溯源字段。
退出码 0 = 全绿。每次运行会埋一个唯一编号提问，避免被库里历史文档干扰。

## 数据模型（6 张表）

`users` / `sessions` / `messages` / `kb_documents` / `kb_chunks` / `followup_task_drafts`
详见 `随访智能体技术架构方案.md` 第 4 节。

## 目录

```
followup-agent/
├── app/
│   ├── main.py
│   ├── core/                 # config / security(JWT+PBKDF2) / database / deps(RBAC)
│   ├── api/v1/               # auth · chat · kb · tasks · reports
│   ├── models/               # 6 张表 ORM
│   ├── schemas/              # Pydantic DTO
│   ├── agents/               # ★ 智能体层
│   │   ├── gateway/          #   base(抽象) · openai_provider · mock_provider · embedding
│   │   ├── prompts/          #   policy_qa.md（带版本号，可评审可 diff）
│   │   ├── rag.py            #   混合检索 + 提示词拼装
│   │   └── guardrails.py     #   输出护栏
│   └── services/             # knowledge_service · chat_service · qa_service（编排）
├── alembic/                  # env.py（URL 从 Settings 注入）+ versions/0001
├── scripts/smoke_test.py     # 端到端冒烟
├── docker-compose.yml        # pgvector/pgvector:pg16（5433）
└── .env.example
```

## 运维提醒

- `/health` 里的 `llm_provider` / `embed_provider` 是排障第一现场：**mock 状态下回答质量无意义**。
- `data/kb/` 存上传的原始制度文件（已 gitignore），备份时别只备数据库——切片是派生物，原文才是凭据。
- 单次检索最多扫描 `RAG_MAX_CANDIDATES=5000` 个切片。超过这个量级应改 pgvector HNSW 召回 topN + BM25 重排，
  替换 `app/agents/rag.py::_score_all` 一个函数即可。

## 下一步（D5–D6）

- **D5**：对话内 function calling 生成随访任务草稿（`followup_task_drafts` 只到 draft，confirmed 必须人工点）
- **D6**：质控报表（完成率、红旗升级率、无引用回答占比——`flags` 字段已就位）
