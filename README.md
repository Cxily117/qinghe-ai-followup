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
| `render_*.py` + `*.html` | 流程图/架构图源文件与渲染脚本（可改可重出图） |

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
