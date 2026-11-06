# 智能体执行轨迹（三条典型对话）

本文是 `app/agents/harness.py` 循环的**预期行为说明书**，也是 `scripts/trajectory_test.py` 的断言依据。
每条轨迹都写清四件事：模型每轮决策、调用的工具、状态变化、最终落库结果。

> 说明：示例里的模型输出以 mock 为准（`LLM_PROVIDER=mock`）。接真实模型后**措辞会变，工具序列不应变**——
> 序列是代码结构决定的（动态工具集 + 票据闸门），不是提示词"请求"出来的。

---

## 轨迹 A：只查规范，不碰业务工具

**输入**：「心梗患者出院 30 天随访要问哪些内容？」

**为什么这样走**：这是纯知识问题，没有具体患者，也没有排程意图。
一旦模型"顺手"去 `resolve_patient`，就会把制度问答拖成患者业务操作，
既慢又扩大 PHI 接触面。所以预期的第一跳就是检索。

| 轮 | 模型决策 | 工具 | 参数（脱敏后） | 状态变化 |
|---|---|---|---|---|
| 1 | tool_call | `search_policy` | `{"query":"心梗患者出院 30 天随访要问哪些内容"}` | `citations` 增加 3 条，flags += `searched` |
| 2 | final | — | — | 结束，status=`succeeded` |

**SSE 事件**：

```
event: run        {"run_uid":"...","mode":"bounded"}
event: step       {"seq":1,"tool":"search_policy","status":"start","label":"正在检索随访制度…"}
event: step       {"seq":1,"tool":"search_policy","status":"ok","detail":{"hits":3}}
event: citations  {"citations":[{"n":1,"doc_title":"冠心病出院随访制度（演示）",...}]}
event: delta      {"text":"根据《…》第 30 天门诊复查…[1]"}
event: done       {"status":"succeeded","steps":2,...}
```

**审计表（`agent_tool_calls`）**：

| seq | decision | tool | latency | result_digest |
|---|---|---|---|---|
| 1 | tool_call | search_policy | 6ms | `{"hits":[{"n":1,"doc_title":"冠心病出院…` |
| 2 | final | (final) | 0ms | `根据《…》第 30 天门诊复查…` |

**不该发生的事**（测试会断言）：`resolve_patient` / `check_followup_rule` / `create_followup_task_draft` 出现任意一个，都算这条轨迹失败。

---

## 轨迹 B：新建随访（完整写链路）

**输入**：「帮我新建这位冠心病出院患者（住院号 ZY2026004）的随访记录」

**为什么这样走**：有明确写意图，但缺"排几天、依据哪条制度"。
顺序是**强制**的：先拿患者事实 → 拿制度依据 → 结构校验通过（发 `rule_ticket`）→ 才能生成草稿。
创建结果仍然是"待确认单"，不是数据库里的记录。

| 轮 | 模型决策 | 工具 | 关键参数 | 状态变化 |
|---|---|---|---|---|
| 1 | tool_call | `resolve_patient` | `{"patient_ref":"ZY2026004"}` | 得到 `patient_id=ZY2026004`、出院日、方案 `冠心病PCI术后随访` |
| 2 | tool_call | `search_policy` | `{"query":"冠心病PCI术后随访 随访 时间 节点"}` | 命中制度条文，编号 [1]..[3] |
| 3 | tool_call | `check_followup_rule` | `{"items":[30,90,180]}` | 规则引擎判定 `passed=true`，签发 `rule_ticket`（绑定患者+规则指纹，5 分钟） |
| 4 | confirm | `create_followup_task_draft` | `{"rule_ticket":"...","items":[30,90,180]}` | **循环中止**，落 `agent_confirmations`（pending），run=`pending_confirm` |
| — | 人工 | `POST /agent/runs/{uid}/confirm` | `{"action":"confirm"}` | 写 `followup_task_drafts`（status=draft），confirmation=confirmed，run=confirmed |

**第 4 轮是关键**：`create_followup_task_draft` 的工具执行**不写库**，
它只做"验票 → 归一化 → 返回预览"。Harness 看到 `pending_confirm` 立即停止循环，
把草稿内容通过 `confirm_required` 事件推给前端。模型没有"确认"这个工具，
也没有 HTTP 能力，所以**它无法自己完成确认**。

**SSE 里会多一个事件**：

```
event: step             {"seq":4,"tool":"create_followup_task_draft","status":"start","label":"正在创建随访草稿…"}
event: confirm_required {"confirmation_id":12,"run_uid":"...","draft":{"patient_ref":"ZY****04","plan_type":"冠心病PCI术后随访","items":[{"offset_days":30,"due_date":"2026-10-31",...}]},"expires_in":600}
event: delta            {"text":"草稿已生成，等待医护确认后才会写入系统；确认前请勿声称已创建…"}
event: done             {"status":"pending_confirm","awaiting_confirm":true,...}
```

**确认接口的三种结果**：

| 请求 | 结果 |
|---|---|
| 本人 confirm（10 分钟内） | 200，`draft_id`，草稿 status=`draft`，`content.ai.confirmed_via=agent_confirm_card` |
| 再次 confirm | 409 `AGENT_CONFIRM_CLOSED`（run 已不是 pending_confirm） |
| 他人 confirm | 404 `AGENT_RUN_NOT_FOUND`（不暴露存在性） |
| 超过 10 分钟 | 409 `AGENT_CONFIRM_EXPIRED`，确认单置 expired，**不落库** |

**失败分支（同样重要）**：

- 输入里没有住院号 → 第 1 轮不调工具，直接问："请提供患者的住院号或门诊号……"。不猜、不编。
- 制度里查不到该方案 → `check_followup_rule` 返回 `passed=false` + `NO_POLICY_EVIDENCE`，
  第 4 轮变成"说明违反了哪条"，**不会**出现草稿。
- 方案缺必检节点（例如只排了第 7 天）→ `MISSING_REQUIRED_NODE`，同样不建草稿。
- 模型想跳过校验直接建（幻觉调用）→ 工具根本没下发，网关回 `tool_unavailable`，
  该轮 `decision=blocked`，flags += `blocked_tool_call`——这是审计里最有价值的一类记录。

---

## 轨迹 C：红旗症状（先救命，再讲条文）

**输入**：「患者说持续胸痛，需要怎么处理？」

**为什么这样走**：胸痛属于可能致命的主诉，处理顺序不能是"先检索制度再判断风险"。
所以第一跳是 `assess_red_flag`（纯规则关键词判定，不做语义猜测），
命中 high 就签发 `risk_ticket`，回答必须给出制度要求的处置动作，**不得**把这种情况排成普通随访节点。

| 轮 | 模型决策 | 工具 | 关键参数 | 状态变化 |
|---|---|---|---|---|
| 1 | tool_call | `assess_red_flag` | `{"symptoms":"患者说持续胸痛，需要怎么处理？"}` | `level=high`，flags += `red_flag_high`，签发 `risk_ticket` |
| 2 | tool_call | `search_policy` | `{"query":"红旗症状 处置 升级 发热 胸痛"}` | 拿到制度处置条文（编号 [1]） |
| 3 | final | — | — | 回答含"立即联系主管医生/胸痛中心 + 记录接手人" |

**预期回答（mock 版）**：

> 这是高危红旗症状，先按制度处置：立即停止问卷，10 分钟内联系主管医生/胸痛中心，必要时拨打急救电话；
> 不得让患者自行前往。请立即电话联系主管医生并记录接手人与时间；随访问卷暂停。[1]

**为什么 `assess_red_flag` 不要求先解析患者**：抢救性判断不能被"还没查到住院号"卡住。
患者信息只影响升级工单归属；没有患者也要能给出风险结论。

**升级分支**：命中 high 后，模型可以继续调 `escalate_to_clinician`（持 `risk_ticket`，
一次性消费），生成 `ESC-xxxx` 升级工单编号。当前演示版只记录工单号与原因，
未接院内通知通道（见 README「已知边界」）。

**不该发生的事**：出现 `create_followup_task_draft` 即失败——红旗症状不能被排成普通问卷。

---

## 一轮决策日志里有什么

每轮落 `agent_tool_calls` 一行（迁移 0004 后）：

| 字段 | 含义 | 为什么需要 |
|---|---|---|
| `seq` | 第几轮 | 顺序即因果，跳步要能看出来 |
| `decision` | `tool_call` / `blocked` / `final` / `confirm` | 区分"调了工具"和"想调但被拦" |
| `tool` | 工具名（final 轮记 `(final)`） | 统计工具使用分布 |
| `args_redacted` | 脱敏入参 | 复盘查了什么，但不留 PHI |
| `model_note` | 模型本轮伴随工具说的话（截断） | 看出"它当时以为自己在干什么" |
| `latency_ms` | 工具执行耗时 | 找慢工具、做容量规划 |
| `result_digest` | 结果摘要（脱敏，250 字） | 不存全文，但足够回放 |
| `ticket_id` | 票据指纹（写操作才有） | 对账"这张草稿拿哪张票建的" |

`GET /api/v1/agent/runs/{run_uid}` 会把上述字段按 `rounds` 返回，并附 `confirmations` 状态。

---

## 三条轨迹的自动化验证

```bash
python scripts/trajectory_test.py --base-url http://127.0.0.1:8000
```

它会真实跑一遍三条轨迹并断言：工具序列、`confirm_required` 是否出现、
确认前库里没有草稿、确认后只新增一张且 status=draft、重复/越权确认被拒、
红旗场景不生成草稿且带 `risk_ticket`。退出码 0 = 全部符合预期。
