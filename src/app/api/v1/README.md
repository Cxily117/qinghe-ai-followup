# api/v1/ —— REST 接口（含人工确认/审核点）

交付清单「至少 3 处人工确认或审核点」在本目录的落点（实际 5 处）：

1. `tasks.py` → `POST /{draft_id}/confirm` —— 生成任务止步草稿，必须归属人确认（AI 无法调用）
2. `knowledge.py` → `publish_document` —— 制度发布审核（草稿不进检索）
3. `knowledge.py` → `archive_document` —— 制度归档/下架审核
4. `agent.py` → `POST /runs/{run_uid}/confirm` —— 智能体待落库草稿的人工确认
5. `prompts.py` 保存校验 —— 提示词变更 422 拦截（另含审计人工标记）

所有管理接口挂 `core/deps.py` 的 `require_admin`，普通员工越权 403。
