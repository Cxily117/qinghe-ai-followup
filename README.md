# 清禾 · 医院随访智能体系统（qinghe-ai-followup）

面向医院出院患者随访场景的智能体系统：制度知识库问答（RAG）、随访任务管理、红旗症状升级闭环、质控度量。架构按全院级平台设计，落地分期走。

## 仓库导航

| 目录/文件 | 说明 |
|---|---|
| `discovery-report.md` | 需求发现报告（现状访谈事实基线，f 编号来源） |
| `随访系统需求清单.md` | 一页纸需求清单（10 问评审确认版）★ 从这里开始 |
| `随访智能体技术架构方案.md` | 四层技术架构 + 表设计 + 周计划 |
| `随访系统设计方案.md` | 系统设计方案（12 章，需求侧） |
| `随访系统行业分析.docx` | 行业分析（政策/市场/竞品/合规） |
| `随访系统报价单.md` / `.html` | 报价单（基础费 + 效果浮动） |
| `随访现状流程图*.png/html` | 现状流程泳道图 / 判断框流程图 |
| `随访现状_AI数智化流程图.png` | 目标态 AI 流程图 |
| `随访系统架构图.png` | 系统分层架构图 |
| `随访现状流程图与分析报告.docx` | 流程图 + 三维分析合并报告 |
| `prototype/` | 阶段 1 RAG 命令行原型（见下） |
| `followup-agent/` | **阶段 2 FastAPI 工程**（认证 + 知识库入库 + 流式 RAG + 任务草稿，见下） |
| `render_*.py` + `*.html` | 流程图/架构图源文件与渲染脚本（可改可重出图） |

## 阶段 2 工程（followup-agent）

FastAPI（异步）+ SQLAlchemy 2.0 + Alembic + JWT + RAG。开发态 SQLite 零依赖可跑，生产切 PostgreSQL+pgvector。

| 里程碑 | 内容 | 状态 |
|---|---|---|
| D1–D2 | 认证（JWT/PBKDF2）+ RBAC + 6 张表 + 双方言迁移 | ✅ |
| D3–D4 | 知识库入库（切片→向量化→发布）+ `/chat` 流式 RAG（混合检索 + 护栏 + 溯源） | ✅ |
| D5 | 任务草稿生成（function calling，AI 只到 draft，confirmed 必须人工点） | ✅ |
| D6 | 质控报表（完成率 / 红旗升级率 / 无引用回答占比 / AI 草稿修改率） | ⬜ |

```bash
cd followup-agent
pip install -r requirements.txt
alembic upgrade head            # 建 6 张表
uvicorn app.main:app --reload   # http://127.0.0.1:8000/docs

# 端到端冒烟（另开终端）
python scripts/smoke_test.py --base-url http://127.0.0.1:8000
```

生产（pgvector）：`docker compose up -d` → `.env` 设 `DATABASE_URL=postgresql+asyncpg://followup:followup@localhost:5433/followup` → `alembic upgrade head`。

默认 `mock` 供应商（无 Key 也能跑通全链路）；接真实模型只改环境变量：
`LLM_PROVIDER=openai` + `EMBED_PROVIDER=openai` + `LLM_API_BASE/LLM_API_KEY`。

已实测：D1–D2 的 14 项接口回归；D3–D4 的冒烟脚本在 **mock 与真实模型（glm-4-flash + embedding-3）两种模式下全绿**；
D5 冒烟新增 5 组断言（客户端硬塞 `status=confirmed` 仍落 draft、缺患者标识 422、越权确认 404、
人工确认 confirmed、重复确认/取消 409）；
**医护问答前端**（单页 + 打字机 + 「正在检索随访规范…」状态 + 引用卡片可展开原文）已用无头浏览器实截图验证，
错误统一为 `{code, message, request_id}` 中文 envelope。冒烟 16 步 26 项全绿。
草稿不参与检索、发布后可命中并带引用、SSE 事件完整、`messages.meta` 溯源字段落库。
详细设计取舍见 `followup-agent/README.md`。


## 阶段 1 原型快速开始

命令行 RAG 问答：读取随访制度 markdown → 500 字切片 → BM25 + 向量混合检索 → LLM 回答（带引用、拒答护栏）。

```bash
cd prototype
pip install faiss-cpu numpy rank_bm25 jieba httpx

# 离线联调（无需 API Key）
python rag_cli.py 随访制度示例.md

# 接真实模型——智谱 GLM（已实测，OpenAI 兼容接口）
# 注意：LLM 和 Embedding 两个供应商开关都要设为 openai
# PowerShell:
$env:LLM_PROVIDER="openai";   $env:EMBED_PROVIDER="openai"
$env:LLM_API_BASE="https://open.bigmodel.cn/api/paas/v4"
$env:LLM_API_KEY="<你的Key>"
$env:LLM_CHAT_MODEL="glm-4-flash"; $env:LLM_EMBED_MODEL="embedding-3"
python rag_cli.py 随访制度示例.md

# 任意 OpenAI 兼容服务同理，改 API_BASE / 模型名即可
```

**真实模型实测记录**（glm-4-flash + embedding-3）：
- 关键词问「骨科术后随访节点」→ 准确回答第 3/7/30 天，引用正确；
- 语义问「患者发烧到 39 度」→ 红旗症状片段融合分 1.00（原文为"体温超过38.5℃"，字面不重合），回答完整复述升级四步流程；
- 超范围问「化疗后用什么药」→ 正确拒答「该问题超出当前知识库范围」，不编造、不标引用；
- 结论：免费档 glm-4-flash 对制度问答场景够用。

六个核心模块（切片 / Embedding 客户端 / FAISS 向量库 / 混合检索器 / LLM 网关 / 提示词构建）均标注了阶段 2 的 FastAPI 工程迁移目标，详见《随访智能体技术架构方案.md》第 6 节。

## 项目文档集（按阅读顺序）

1. **需求清单** → 定范围、定口径
2. **技术架构方案** → 定技术栈、定表结构、定里程碑
3. **设计方案 / 行业分析 / 报价单** → 立项与商务材料
4. **流程图三张** → 现状 → 目标态 → 系统架构
5. **prototype/** → 阶段 1 已验证的最小实现

## License

内部项目文档，未授权前请勿外传。
