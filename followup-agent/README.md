# followup-agent · 阶段 4（D1–D5 + 管理后台 + 智能体 P1）

FastAPI（异步）+ SQLAlchemy 2.0 + Alembic + JWT + RAG + function calling + 原生 JS 前端 + Jinja2 管理后台。

- **D1–D2**：认证（JWT/PBKDF2）+ RBAC + 6 张表 + 双方言迁移 ✅
- **D3–D4**：知识库入库（切片→向量化→发布）+ `/chat` 流式 RAG（混合检索 + 护栏 + 溯源落库）✅
- **D5**：对话生成随访任务草稿（function calling，**AI 只到 draft，confirmed 必须人工点**）✅
- **阶段 3**：管理后台（知识库管理 / **提示词在线编辑、保存即生效** / 对话审计与高危标记）✅
- **阶段 4 P1**：自主随访智能体（只读工具链 + 动态工具解锁 + 票据地基 + 工具步骤时间线 + 运行回放审计）✅
- **阶段 4 P2/P3**：执行循环（5 轮 + 逐轮决策日志）/ 合规校验闸门 / **创建草稿强制中断等人工确认** / 红旗评估与升级 ✅
- **D6**：质控报表（六组指标 API + CSV 导出 + 后台 `/admin/reports` 页面）✅

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
>
> ```bash
> python scripts/reindex_embeddings.py --all      # 用当前 embedding 重算全部切片向量
> python scripts/reindex_embeddings.py --dry-run  # 先看会动哪些切片
> ```

**已接入的真实模型（2026-10-30 验证）**：智谱 `glm-4-flash` + `embedding-3`（1024 维），
对话与向量化共用一个 Key。启动日志会打印生效配置（provider / model / base / key 是否就位），
排障时先看这一行。

### 一个 Key 能不能一直用？能，但要守住四条

1. **只放在 `.env`**。`.env` 已 gitignore，`.env.example` 里只留空占位符 —— Key 不进代码、
   不进文档、不进提交记录。日志与审计也做了脱敏（`_redact_args` 会先掩码再截断）。
2. **开发与生产分两个 Key**。不是怕串数据，是为了能单独限额、单独作废：一个泄漏或欠费时，
   另一个还能跑。已经贴到聊天/截图里的 Key 一律当作已泄漏，去控制台作废重建。
3. **额度与并发看平台侧**。`glm-4-flash` 本身免费但仍有 QPS 上限，`embedding-3` 按 token 计费。
   免费额度用尽的表现就是 `429` —— 这时页面上只会显示「大模型连接失败」，别急着改代码。
4. **换 Key 是一个有顺序的动作**，顺序错了会白折腾：

```bash
# ① 写新 Key 到 .env（或改环境变量）
# ② 体检：配置通不通、Key 有没有效、向量维度对不对，一条命令 5 秒出结论
python scripts/check_llm.py --verbose
# ③ 只有「换了 embedding 模型 / 改了 EMBED_DIM / 换了向量供应商」才需要重建向量
python scripts/reindex_embeddings.py --all
# ④ 重启服务，确认启动日志里 key 显示「已配置」
uvicorn app.main:app --port 8000
# ⑤ 最后去旧 Key 的控制台作废
```

`check_llm.py` 会把 `401 / 402 / 403 / 404 / 429` 各自翻译成一句能直接动手的话，
并附上游返回的原文，省得对着「连接失败」四个字猜。

### 真实模型下必须靠代码兜住的三件事

mock 不会幻觉、不会自作主张，所以这些坑只在接真模型后暴露，都已在代码里做确定性拦截：

| 风险 | 真实表现 | 拦截点 |
|---|---|---|
| **节点外推** | 问「第 45 天要做什么」，条文只写了 3/7/30 天，模型顺口编出一套并标引用 | `guardrails.detect_node_extrapolation`：问题问的时点不在任何检索条文中 → 强制改写成「未查询到相关内容」并列出制度实际覆盖的节点 |
| **编造患者标识** | 「出院后第 7 天打电话」这种没有患者信息的请求，模型自己给出 `P123456` 并建草稿 | `task_service._grounded`：模型给的标识必须在原始对话里原样出现过，否则丢弃并 422 |
| **长答案夹带医学建议** | 模型主动补「建议服用 XX、剂量 YY」 | `guardrails.contains_medical_advice`：命中即打 `medical_advice_suspected` 并追加免责，绝不改写结论 |

`scripts/trajectory_test.py` 断言的是 mock 的固定工具序列，接真模型后会自动跳过
（真实模型可能合理走别的路径），需要时用 `--force` 强制跑。

## 生产态（PostgreSQL 16 + pgvector）

```bash
docker compose up -d          # pgvector/pgvector:pg16 → 端口 5433（避让本机 PG13）
# .env: DATABASE_URL=postgresql+asyncpg://followup:followup@localhost:5433/followup
alembic upgrade head          # 自动 CREATE EXTENSION vector + vector(1024) + HNSW 索引
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 后台页面（Jinja2 服务端渲染，仅 admin）

| 路径 | 页面 | 说明 |
|---|---|---|
| `GET /admin/login` | 登录 | 表单登录写 HttpOnly Cookie；**staff 在此被拒**并提示去问答端 |
| `GET /admin` | 概览 | 已发布文档 / 可检索切片 / 近 7 日问答量 / 待复核高危会话 + 最近预警 |
| `GET /admin/kb` | 知识库 | 上传（md/txt/pdf，拖拽）· 筛选 · 表格行操作（查看/发布/归档/删除） |
| `GET /admin/kb/{id}` | 文档详情 | 元信息 + 切片列表（可折叠）+ 原文下载 + 发布 |
| `GET /admin/prompts` | 提示词 | 左侧模板列表 / 右侧编辑器 + 版本历史（可回滚、可看 diff）+ 试跑 |
| `GET /admin/audit` | 对话审计 | 医护、时间段、仅疑似、仅已标记筛选 + 风险标签 |
| `GET /admin/audit/{session_id}` | 会话详情 | 逐条问答 + flags + 引用来源 + 右侧标记面板（留操作人痕迹） |
| `GET /admin/users` | 用户与授权 | 全部账号一览（角色/科室/状态/注册时间）；授予或回收管理员、启用或停用 |

访问控制：Cookie 里的 JWT 校验 `role=admin`；staff 携带有效 token 访问页面 → 403 页并引导至问答端。
接口层同样由 `require_admin` 兜底，**页面和接口是两道独立闸门**，任何一道都不依赖前端隐藏按钮。

### 角色与授权（管理员系统）

- **注册只发医护**：`POST /auth/register` 的 `role` 字面量收窄为 `staff`，带 `role=admin` 直接 422。
  之前"演示允许自注册 admin"的口子已收掉——谁能自己发自己管理员，权限模型就失效了。
- **管理员身份只有两个来源**：① 已有管理员在后台「用户与授权」页授予；
  ② 冷启动引导——系统一个管理员都没有时，首个注册账号自动成为管理员（仅此一次）；
  忘记密码/唯一管理员被停用时，从服务器本地跑 `python scripts/create_admin.py` 补救（幂等）。
- **授权动作有防呆**（`user_service.set_role / set_status`）：
  不能操作自己的账号；系统始终保留至少一名可用管理员，最后一个 admin 不可降级/停用；
  停用立即生效——登录与页面鉴权都以数据库 `status` 为准，不信任 token。
- 表结构沿用 `users.role / users.status`，无迁移。

## 接口清单

| 方法 | 路径 | 说明 | 权限 |
|---|---|---|---|
| GET | `/health` | 健康检查（含当前 provider） | 公开 |
| POST | `/api/v1/auth/register` | 注册（只发医护；系统零管理员时首个注册自动成为管理员） | 公开 |
| POST | `/api/v1/auth/login` | 登录换取 JWT | 公开 |
| GET | `/api/v1/auth/me` | 当前用户 | 登录 |
| POST | `/api/v1/kb/documents` | 上传制度文档（md/txt/**pdf**，自动切片+向量化，落 draft） | 管理员 |
| POST | `/api/v1/kb/documents/{id}/publish` | 发布（进入检索，旧版自动归档） | 管理员 |
| POST | `/api/v1/kb/documents/{id}/archive` | 归档下架（退出检索，保留切片与原文） | 管理员 |
| DELETE | `/api/v1/kb/documents/{id}` | 删除（仅 draft/archived；已发布 → 409） | 管理员 |
| GET | `/api/v1/kb/documents` | 文档列表（状态/关键词筛选 + 分页） | 管理员 |
| GET | `/api/v1/kb/documents/{id}` | 文档详情 | 管理员 |
| GET | `/api/v1/kb/documents/{id}/chunks` | 查看切片（核对切片质量） | 管理员 |
| GET | `/api/v1/kb/stats` | 知识库统计（已发布切片数） | 管理员 |
| GET | `/api/v1/prompts` | 提示词模板清单（生效版本） | 管理员 |
| GET | `/api/v1/prompts/{name}` | 模板详情（生效正文 + 版本历史） | 管理员 |
| PUT | `/api/v1/prompts/{name}` | **保存新版本并立即生效**（缺「引用/拒答」约束 → 422） | 管理员 |
| POST | `/api/v1/prompts/{name}/rollback` | 回滚（复制旧正文成新版本，版本号不倒退） | 管理员 |
| POST | `/api/v1/prompts/{name}/preview` | 用草稿提示词试答一条问题（不落库） | 管理员 |
| GET | `/api/v1/audit/sessions` | 审计列表（医护/时间段/仅疑似/仅已标记） | 管理员 |
| GET | `/api/v1/audit/sessions/{id}` | 会话明细（消息 + flags + 引用 + 标记） | 管理员 |
| POST | `/api/v1/audit/sessions/{id}/marks` | **标记疑似高危**（记操作人） | 管理员 |
| POST | `/api/v1/audit/marks/{id}/revoke` | 撤销标记（软撤回留痕） | 管理员 |
| GET | `/api/v1/audit/users` | 医护下拉 | 管理员 |
| POST | `/api/v1/chat` | **SSE 流式**问答 | 登录 |
| POST | `/api/v1/chat/ask` | 非流式问答（脚本/兜底） | 登录 |
| POST | `/api/v1/chat/sessions` | 新建会话 | 登录 |
| GET | `/api/v1/chat/sessions` | 我的会话列表 | 登录 |
| GET | `/api/v1/chat/sessions/{id}/messages` | 会话消息（含溯源 meta） | 登录（仅本人） |
| POST | `/api/v1/agent/chat` | **智能体问答（SSE，含 step 工具步骤与 confirm_required 确认事件）** | 登录 |
| GET | `/api/v1/agent/runs/{run_uid}` | 运行回放（逐轮决策 / 脱敏入参 / 结果摘要 / 票据指纹 / 确认单） | 本人/管理员 |
| POST | `/api/v1/agent/runs/{run_uid}/confirm` | **确认或取消待落库的随访草稿**（唯一能写出草稿的智能体路径） | 本人/管理员 |
| GET | `/api/v1/tasks/drafts` | 我的任务草稿（`?status_filter=draft`） | 登录 |
| POST | `/api/v1/tasks` | **生成任务草稿**（function calling，只出 draft） | 登录 |
| GET | `/api/v1/tasks/{id}` | 草稿详情（含 `content.ai` 溯源） | 归属人/管理员 |
| POST | `/api/v1/tasks/{id}/confirm` | **人工确认**（AI 无法调用） | 归属人/管理员 |
| POST | `/api/v1/tasks/{id}/cancel` | 取消草稿（已确认不可取消 → 409） | 归属人/管理员 |
| GET | `/` | **医护问答单页**（web/ 目录，同域托管无 CORS） | 登录后使用 |
| GET | `/api/v1/kb/citations/{chunk_id}` | 引用片段原文（仅 published 文档） | 登录 |
| GET | `/api/v1/reports/summary` | **质控汇总**（`?days=1-365`，六组指标） | 管理员 |
| GET | `/api/v1/reports/summary.csv` | 质控汇总导出 CSV（带 BOM，Excel 直开） | 管理员 |
| GET | `/admin/reports` | 质控报表页（KPI + 漏斗 + 工具 Top10，窗口 7/30/90/180 天） | 管理员 Cookie |

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

`/agent/chat` 在此之上多一种事件：`step`（工具步骤时间线）。

```
event: run         data: {"run_uid":"...","mode":"bounded"}
event: step        data: {"seq":1,"tool":"resolve_patient","status":"start","label":"正在识别患者…"}
event: step        data: {"seq":1,"tool":"resolve_patient","status":"ok","detail":{"found":true,"dept":"骨科"}}
event: step        data: {"seq":2,"tool":"search_policy","status":"blocked","label":"已拦截越权调用"}
```

## 阶段 4：自主随访智能体

`/chat`（固定流程 RAG）与 `/agent/chat`（自主决策智能体）**刻意并存**：
前者链路短、延迟低、可解释，覆盖绝大多数制度问答；后者能查患者、能多次检索、能判合规，
但延迟更高、路径不固定。两条路可一键切换，也便于 A/B 对比。

### 为什么自建 Harness，而不是 LangGraph / AutoGen / Dify

这一个流程用框架是负收益，四个理由（代码见 `app/agents/harness.py` 文件头）：

1. **闸门必须是能力裁剪，不是图上的一条边。** 我们要的是"没校验就看不到创建工具" + 票据验签。
   图的边可以绕过（模型照样能调工具），最终还得在工具内部再写一遍，框架只是多一层抽象。
2. **审计字段是我们自定义的。** `decision / args_redacted / result_digest / ticket_id` 是为"能向医务处举证"设计的，
   框架的 trace 格式要落成这些字段仍需大量映射。
3. **院内环境要可解释、可离线。** 自建 Harness 约 200 行、零新依赖；引入框架要多 20+ 个包和一套概念。
4. **降级要一键。** `AGENT_MODE=strict` 只下发只读工具，在自建里是一个参数，在框架里要重画一张图。

代价也写清楚：没有现成的 checkpoint/断点续跑、并行工具、可视化 trace。本流程 ≤5 轮、串行、单写操作，用不上。
将来做多智能体协作时再换框架不迟——`dispatch` 与 `RoundRecord` 两个契约是稳定的，替换的是执行器不是业务。

### 执行循环：最多 5 轮，每轮一条决策日志

```
模型决策 ─► 有 tool_calls ─► 校验工具可见性 ─► 执行 ─► 结果回填 ─┐
   ▲                                                            │
   └────────────────────────────────────────────────────────────┘
          └─ 无 tool_calls ─► 最终答复（结束）
          └─ 写操作返回 pending_confirm ─► 挂起等人工确认（结束）
```

- 一轮只执行一个工具：顺序约束要求"一步一验证"，并发会打乱因果链。
- 第 5 轮仍在调工具 → 直接转人工（`max_steps`），不无限烧钱。
- **每轮落 `agent_tool_calls` 一行**：`decision`（tool_call / blocked / final / confirm）、
  工具名、脱敏入参、模型本轮原话、耗时、结果摘要、票据指纹。这是审计分析的数据源。

### 写操作闸门：创建草稿前强制中断

`create_followup_task_draft` 的执行体**不写库**，只做三件事：验票（一次性）→ 归一化草稿 → 返回预览。
Harness 看到 `pending_confirm` 立刻停止循环，把草稿内容以 `confirm_required` 事件推给前端卡片；
草稿内容存进 `agent_confirmations`（pending，10 分钟有效），run 置 `pending_confirm`。

只有 `POST /agent/runs/{uid}/confirm` 能把它变成 `followup_task_drafts`（**状态仍是 draft**）。
模型既没有确认工具、也没有 HTTP 能力，"自己确认自己"在结构上不成立——
这比在提示词里写十遍"不要自动确认"可靠。

拒绝"先建 draft 再标待确认"：草稿一旦进库就会被任务列表读走，确认前不存在更干净。

### 三条典型轨迹

`docs/agent-trajectories.md` 给出完整预期轨迹（每轮决策 / 工具 / 参数 / SSE / 审计行）：

| 轨迹 | 输入 | 预期工具序列 |
|---|---|---|
| A 只查规范 | 心梗患者出院 30 天随访要问哪些内容 | `search_policy` |
| B 新建随访 | 帮我新建这位冠心病出院患者的随访记录 | `resolve_patient → search_policy → check_followup_rule → create_followup_task_draft`（然后挂起等确认） |
| C 红旗症状 | 患者说持续胸痛，需要怎么处理 | `assess_red_flag → search_policy`（不建草稿） |

`scripts/trajectory_test.py` 会把这三条真跑一遍并断言序列与落库行为。

### 工具清单与调用顺序约束

| 工具 | 类别 | 说明 |
|---|---|---|
| `resolve_patient` | 读 | 住院号/姓名 → 患者档案（mock 患者库） |
| `search_policy` | 读 | 制度检索（允许模型改写 query 重查，单轮最多 3 次） |
| `list_existing_tasks` | 读 | 查该患者已有随访任务，防重复建 |
| `assess_red_flag` | 读 | 红旗症状判定（P3 接升级链路） |
| `check_followup_rule` | 判定 | 对照制度判定方案合规，**通过后签发 rule_ticket**（规则见 `rules.py`） |
| `create_followup_task_draft` | 写 | **持票 + 二次人工确认**：先挂起成确认单，人点了才落库（status=draft） |
| `escalate_to_clinician` | 写 | 升级专科医生，需 risk/rule_ticket（一次性消费） |
| `handoff_to_human` | 兜底 | 无法处理时转人工 |

**顺序约束不靠提示词自觉，靠三层机制**：

1. **动态工具集**：每轮按状态机算出模型"此刻能看见的工具"。未过校验时，
   `create_followup_task_draft` 根本不在 tool schema 里下发——模型没有语法去调它。
   若模型仍试图调用（幻觉工具名），网关返回 `tool_unavailable` 并把 `blocked_tool_call` 写进 flags：
   这就是"试图跳步"的审计证据。
2. **HMAC 签名票据**：`check_followup_rule` 通过才签发 rule_ticket（5 分钟过期 + nonce 一次性 +
   内嵌 patient_id/plan_type + 绑定制度版本 sha）。写工具入参必须携带票据，服务端验签，
   不接受"模型口头说已通过"。
3. **系统提示词硬约束**：`app/agents/prompts/agent_supervisor.md` 写明顺序强制段
   （"创建前必须已收到校验通过"）、写操作只到草稿、调用前自检三句——作为第 1/2 层的补充而非替代。

刻意**没有** `confirm_task` / `cancel_task` 工具：确认永远是人点按钮，与 D5 的状态机一脉相承。

### 审计与回放

- 每个工具调用落 `agent_tool_calls`：**脱敏后的入参**（住院号/姓名打码、长文本截断、
  query 内嵌住院号也先掩码）+ 结果摘要（sha256 摘要而非原文）+ 票据指纹 + 耗时。
  审计表不成为第二个 PHI 泄露面。
- `GET /agent/runs/{run_uid}` 逐轮回放：调了什么工具、带了哪张票据、返回什么摘要。
  本人可看自己的，管理员可看全部；他人访问一律 404（不暴露存在性）。
- 三张新表（迁移 `0003`，不改旧表）：`agent_runs` / `agent_tool_calls` / `ticket_nonces`。

### 前端：工具步骤时间线

问答单页新增「标准问答 / 智能体」分段切换。智能体模式下，每个 `step` 事件实时渲染成时间线节点
（开始 → 完成/拦截），最终答案仍走打字机；回看历史时时间线从 `meta.agent` 还原。

### 智能体 SSE 收口约定（踩过的坑）

最终答案此前没有流式吐过字，收口时**只发一个事件**：护栏改写过 → `replace`，未改写 → `delta`。
两个都发会导致前端把整段答案显示两遍（`delta` 是追加语义）——冒烟 [22] 已加回归断言。

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

## 医护问答前端（`web/`，无构建）

单页三件套（HTML + 原生 JS + CSS），FastAPI 同域托管在 `GET /`，登录后直接用。

| 设计 | 为什么 |
|---|---|
| **不用 EventSource，用 fetch + ReadableStream 手解 SSE** | EventSource 带不了 `Authorization` 头；顺带能统一处理 401 跳登录 |
| **打字机走帧队列**（`requestAnimationFrame` 每帧 2–5 字，随积压量加速） | 上游一个 chunk 可能几十字，直接 append 是"跳字"不是"打字" |
| **`replace` 事件整体覆盖气泡** | 护栏删过引用编号后，屏幕上不能残留旧的 `[1]` |
| **检索态文案锚在事件上**：`session` 显示「正在检索随访规范…」，`citations`/首个 `delta` 收起 | 这段时间正好是 BM25 + 向量打分；超过 1.5s 追加"仍在检索"防误判卡死 |
| **历史消息引用存 `meta.citation_detail`**（完整对象，不只编号） | 切换会话回看时也能渲染"文件名 + 片段"，否则只剩 `[1][2]` 两个数字 |
| **多轮上下文按「轮」取 6 轮**（`recent_history(rounds=6)`） | 旧实现按条取 6 条 = 3 轮，上下文短一半；截断点若落在 assistant 上会去掉半轮 |

## 统一错误 envelope

所有 4xx/5xx 返回 `{"code","message","request_id"}`；`code` 是稳定标识（前端按它分支），`message` 直接可展示，`request_id` 用于报障定位日志。

| code | HTTP | message |
|---|---|---|
| `AUTH_REQUIRED` | 401 | 未登录或登录已过期 |
| `SESSION_NOT_FOUND` | 404 | 会话不存在或无权访问（用 404 不用 403：不暴露"会话存在但不属于你"） |
| `QUESTION_EMPTY` / `QUESTION_TOO_LONG` | 422 | 问题不能为空 / 最多 2000 字 |
| `CHUNK_NOT_FOUND` | 404 | 引用片段不存在，或所属文档已归档 |
| `LLM_UPSTREAM` | 502 | 模型调用失败，请稍后重试 |
| `INTERNAL` | 500 | 服务内部错误，已记录日志 |

规则：**SSE 已开始输出后**（HTTP 200 已发出）错误只能走 `event: error`，`code` 与上表同名。
注意"知识库为空"不是错误——它是业务上的拒答，返回 200 + 拒答话术且不标引用（合规线，不能变成 503）。

## D5 的关键设计（任务草稿）

| 设计 | 为什么 |
|---|---|
| **工具 schema 里没有 `status` 字段** | 「AI 只能写草稿」最硬的实现不是提示词，而是让模型**没有语法**表达"已确认"；服务端 `status="draft"` 是写死的字面量 |
| **入参只收 `offset_days`，不收日期** | 日期计算是模型最不可靠的能力；"出院后第 7 天"由服务端 `base_date + timedelta` 算，并统一归一到 **09:00** |
| **抽不到患者标识就 422，不编造** | 编一个住院号写进库里，比生成失败严重得多；允许调用方传 `patient_ref` 兜底，并在 flags 里标记 `patient_ref_from_hint` |
| **节点清洗在代码里**：0–365 钳制、按天去重合并、最多 10 条、渠道白名单 | 模型会"好心"补出对话里没有的节点、会输出 `第7天` 这种字符串 |
| **确认时可改方案/时间/节点** | "确认"的语义是"具名的人看过后按自己的判断定稿"，不是"同意 AI"；`content.confirm` 记确认人与时间 |
| **24h 内同患者同节点去重** | 防止前端连点刷出一串重复草稿卡片（返回 `reused: true`） |
| **`chat_tools` 走网关 + JSON 降级** | 院内换成本地小模型（vLLM/Qwen 7B）时 tools 支持不稳；上游 400/404/422 时自动降级为"只输出 JSON"，整条链路不中断 |
| **原始 `raw_args` 落库** | 归一化会改模型输出，出问题时必须能对账（`content.ai.raw_args`） |

### 草稿状态机

```
POST /tasks            → draft ──confirm（人工）──→ confirmed（终态）
                            └────cancel（人工）───→ cancelled（终态）
```

`confirmed` 不可再确认（409）、不可取消（409）；非归属人操作一律 404（不暴露"存在但你没权限"）。

### 草稿落库结构

```json
{
  "items": [{"name": "出院后第 3 天随访", "offset_days": 3, "channel": "phone",
             "notes": "", "due_date": "2026-10-23"}],
  "rationale": "依据对话中的「第 3/7/30 天电话随访」",
  "ai": {"model": "glm-4-flash", "prompt_version": "v1", "latency_ms": 1830,
         "degraded": null, "tool": "create_followup_task_draft",
         "raw_args": {"...": "模型原始入参"}, "flags": {}, "trigger": "manual"},
  "confirm": {"by": "nurse01", "by_id": 2, "at": "2026-10-30T11:02:33+08:00",
              "note": "已核对出院日", "ai_item_count": 3}
}
```

## 冒烟测试（可挂 CI）

```bash
uvicorn app.main:app --port 8000 &
python scripts/smoke_test.py --base-url http://127.0.0.1:8000        # 27 段 81 断言：接口与页面
python scripts/trajectory_test.py --base-url http://127.0.0.1:8000   # 三条轨迹：过程与闸门
```

覆盖：健康检查 → 注册登录 → 上传(draft) → **发布前提问不命中该文档** → 发布 →
**发布后命中并引用** → SSE 事件完整性 → `messages.meta` 溯源字段 →
**生成任务草稿（客户端硬塞 `status=confirmed` 也必须落 draft）** → 缺患者标识 422 →
越权确认 404 → 人工确认 confirmed → 重复确认/取消已确认 409 →
**错误 envelope（空问题/超长/越权会话的 code 与中文 message）** →
**引用原文接口（医护可读 published、不存在返回 CHUNK_NOT_FOUND）** →
**前端托管（GET / 单页 + /static/app.js）** →
**管理后台（staff 登后台被拒 / 接口 403 / 上传 md 切片 / 删除已发布 409 / 提示词保存新版本 / 缺约束 422 / 问答 meta 记录新版本 / 审计筛选疑似 / 标记与撤销 / 各页面渲染 200）** →
**智能体（SSE 事件完整 / 最终答案只发一个事件 / final_text 无重复 / 回放工具链首个必为
search_policy / 审计入参不含原始住院号 / 他人 run 404）** →
**质控报表（六组指标齐全 / days 钳制 422 / staff 403 / CSV 带 BOM / 后台页面渲染 / 越权 403）** →
**用户与授权（注册 role=admin 422 / 默认 staff / 授予与回收管理员 / 不能操作自己 / 停用立即无法登录 / 启用恢复）**。
退出码 0 = 全绿（27 段 81 断言）。每次运行会埋一个唯一编号提问，避免被库里历史文档干扰。

### D6 质控指标口径（六组）

| 组 | 关键指标 | 口径 |
|---|---|---|
| `qa` | 无引用回答率 | 窗口内 assistant 消息（有 meta）：非拒答且 `meta.citations` 为空 ÷ 总回答数 |
| `qa` | 护栏改写率 | `meta.flags` 出现任一护栏标志（拒答/剔引用/疑似通用知识…）的占比 |
| `drafts` | 确认率 | `confirmed` ÷ 全部草稿 |
| `drafts` | **AI 人工修改率** | 已确认草稿中，人定稿的节点集合（offset_days）≠ AI 原始输出（`content.ai.raw_args`）的占比——数量没变但节点改天也算改 |
| `agent` | blocked 率 | `agent_tool_calls.decision='blocked'` ÷ 工具调用总数（模型试图越权的频率） |
| `agent` | 确认单漏斗 | pending / confirmed / cancelled / expired 分布 |
| `tools` | 工具 Top10 | 次数、失败率、平均耗时 |
| `marks` | 审计标记 | 窗口内未撤销的 high_risk / watch 计数 |

## 数据模型（12 张表）

前 6 张：`users` / `sessions` / `messages` / `kb_documents` / `kb_chunks` / `followup_task_drafts`
详见 `随访智能体技术架构方案.md` 第 4 节。

阶段 3 新增 2 张（迁移 `0002`，不改旧表）：

- **`prompt_templates`**：`name` / `version` / `content` / `note` / `is_active` / `content_sha` / `created_by` / `created_at`。
  `(name, version)` 唯一；同一 `name` 只有一条 `is_active`（服务层事务保证）。
- **`chat_audit_marks`**：`session_id` / `message_id`(可空) / `level`(`high_risk`|`watch`) / `note` /
  `marked_by` / `created_at` / `revoked_by` / `revoked_at`。「撤销」是软撤回，记录永不删除。

阶段 4 新增 3 张（迁移 `0003`）：**`agent_runs`**（每轮智能体运行：route / steps / flags / prompt_version）、
**`agent_tool_calls`**（逐轮决策：decision / 脱敏入参 / 结果摘要 / 票据指纹）、**`ticket_nonces`**（票据一次性防重放）。
迁移 `0004` 再新增 **`agent_confirmations`**（待确认草稿：payload / 有效期 / 决策人 / 落库草稿 id），
并给 `agent_tool_calls` 补 `decision` / `model_note` 两列。

迁移时写入两版种子：`v1` = 你给的默认模板（生效），`v0` = 原 `policy_qa.md` v3 内容（历史存档）。

## 提示词热生效怎么做的

不靠「改完记得清缓存」这种自觉：

1. `build_messages()` 不再自己读文件，改为**由调用方传入提示词对象**（`PromptSnapshot`）。
2. `prompt_service.get_active()` 每次问答前查一行生效版本（主键索引，成本可忽略）。
3. 每轮回答把 `prompt_version` + 正文 `sha256` 前 8 位写进 `messages.meta` ——
   事后能证明「当时那句回答用的是哪一版提示词」，改模板也不会污染历史记录。
4. 保存前强校验：≥80 字、必须含「引用」与「拒答」语义，否则 422 `PROMPT_MISSING_RULE` 并指出缺哪条
   （**默认模板比原 v3 少了用药、红旗升级、防套取三条硬约束，页面会提示但不擅自改你的模板**）。
5. DB 读不到或内容异常 → 回退文件默认版，页面顶部显示「当前使用兜底模板」。
6. **回滚 = 用旧正文生成新版本**，不是把旧记录设为 active —— 否则版本序列会倒流，审计说不清。

## 目录

```
followup-agent/
├── web/                      # ★ 医护问答单页（index.html + static/{app.js,style.css}，无构建）
├── app/
│   ├── main.py               # 挂载 web/ 与 admin/static、注册统一错误处理器、SSR 模板
│   ├── admin/                # ★ 管理后台（Jinja2）
│   │   ├── routes.py         #   页面路由 + Cookie 鉴权 + 表单动作
│   │   ├── templates/        #   base / login / dashboard / kb_list / kb_detail / prompts / audit_* / error
│   │   └── static/admin.css
│   ├── core/                 # config / security(JWT+PBKDF2) / database / deps(RBAC) / errors(统一错误码)
│   ├── api/v1/               # auth · chat · kb · tasks · reports · prompts · audit · agent
│   ├── models/               # 11 张表 ORM（含 prompt_templates / chat_audit_marks / agent_*）
│   ├── schemas/              # Pydantic DTO
│   ├── agents/               # ★ 智能体层
│   │   ├── harness.py        #   ★ 执行循环（≤5 轮 / 逐轮 RoundRecord / 写操作中断）
│   │   ├── runtime.py        #   状态机 + 票据签发验签 + 一次性消费 + 动态工具解锁
│   │   ├── rules.py          #   ★ 合规规则引擎（必检节点/区间/渠道/红旗词表）
│   │   ├── handlers.py       #   工具执行体（查患者/检索/校验/挂起建草稿/升级）
│   │   ├── gateway/          #   base(抽象，含 chat_tools 契约) · openai_provider · mock_provider · embedding
│   │   ├── prompts/          #   __init__(文件兜底) · policy_qa.md · task_extract.md · agent_supervisor.md
│   │   ├── tools.py          #   8 工具注册表（schema + 分类 + 解锁条件 + 模式裁剪）
│   │   ├── rag.py            #   混合检索 + 提示词拼装（提示词由调用方传入）
│   │   └── guardrails.py     #   输出护栏 + 红旗关键词
│   └── services/             # knowledge · chat · qa · task · prompt · audit · doc_parser · agent
├── alembic/                  # env.py（URL 从 Settings 注入）+ versions/0001–0004
├── docs/agent-trajectories.md  # ★ 三条典型轨迹（含 SSE 与审计行示例）
├── scripts/smoke_test.py     # 端到端冒烟（27 段 81 断言）
├── scripts/create_admin.py   # 管理员引导：本地直连建号/重置密码（幂等）
├── scripts/check_llm.py      # ★ Key/模型体检：配置·对话·向量维度，一条命令出结论
├── scripts/trajectory_test.py  # ★ 三条轨迹的过程断言（真实模型下自动跳过）
├── scripts/reindex_embeddings.py # ★ 换 embedding 后重建切片向量
├── docker-compose.yml        # pgvector/pgvector:pg16（5433）
└── .env.example
```

## 运维提醒

- `/health` 里的 `llm_provider` / `embed_provider` 是排障第一现场：**mock 状态下回答质量无意义**。
- `data/kb/` 存上传的原始制度文件（已 gitignore），备份时别只备数据库——切片是派生物，原文才是凭据。
- 单次检索最多扫描 `RAG_MAX_CANDIDATES=5000` 个切片。超过这个量级应改 pgvector HNSW 召回 topN + BM25 重排，
  替换 `app/agents/rag.py::_score_all` 一个函数即可。

## 下一步（已知边界）

- 真实模型已接入并验证（智谱 glm-4-flash + embedding-3）；Key 只存在本地 `.env`（已 gitignore），
  轮换时改 `.env` → 跑 `scripts/check_llm.py` 体检 → 重启即可，无需改代码
- 升级工单当前只生成编号并落审计（`ESC-xxxxxxxx`），未接院内通知通道/HIS 工单表
- 规则档位目前内置骨科与冠心病两套（`rules.PLAN_PROFILES`），其他病种走"仅结构校验"并标记 `PROFILE_UNKNOWN`
- 报表 meta 细粒度统计在 Python 侧聚合（跨 PG/SQLite 无可移植 JSON 语法）；数据量上来后换 PG jsonb 路径，口径不变
